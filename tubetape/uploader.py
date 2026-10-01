"""YouTube upload with quota tracking and retry.

Each upload costs 1600 quota units (default daily budget 10,000). Uploads are
resumable and retried with exponential backoff; quota-exceeded errors are not
retried (they queue to the next day).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from googleapiclient.http import MediaFileUpload

from .log import get_logger

_logger = get_logger("uploader")

UPLOAD_QUOTA_UNITS = 1600
DAILY_QUOTA_DEFAULT = 10000

DEFAULT_CATEGORY_ID = "22"  # People & Blogs


class QuotaExceededError(RuntimeError):
    """Raised when the daily upload quota is exhausted."""


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@dataclass
class QuotaTracker:
    daily_limit: int = DAILY_QUOTA_DEFAULT
    used: int = 0
    date: str = field(default_factory=_today)

    @property
    def remaining(self) -> int:
        return self.daily_limit - self.used

    def can_upload(self) -> bool:
        return self.remaining >= UPLOAD_QUOTA_UNITS

    def consume(self) -> bool:
        if not self.can_upload():
            return False
        self.used += UPLOAD_QUOTA_UNITS
        return True

    def rollover(self, today: str | None = None) -> None:
        """Reset the counter when the stored date is not today."""
        today = today or _today()
        if self.date != today:
            self.used = 0
            self.date = today

    def to_dict(self) -> dict:
        return {"date": self.date, "used": self.used, "daily_limit": self.daily_limit}

    @classmethod
    def from_dict(cls, data: dict | None) -> "QuotaTracker":
        if not data:
            return cls()
        return cls(
            daily_limit=int(data.get("daily_limit", DAILY_QUOTA_DEFAULT)),
            used=int(data.get("used", 0)),
            date=data.get("date") or _today(),
        )


def build_upload_body(
    title: str,
    description: str,
    privacy: str = "private",
    category_id: str = DEFAULT_CATEGORY_ID,
    made_for_kids: bool = False,
) -> dict:
    """Build the ``videos.insert`` body with all required metadata fields."""
    return {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": made_for_kids,
            "selfCertification": {"selfDeclaredMadeForKids": made_for_kids},
        },
    }


def is_quota_error(exc: Exception) -> bool:
    """Detect a YouTube quota-exceeded error from an HttpError."""
    status = getattr(exc, "status", None)
    content = getattr(exc, "content", None)
    if status == 403 and content:
        try:
            data = json.loads(content.decode("utf-8", errors="replace"))
            for item in data.get("error", {}).get("errors", []):
                if "quota" in str(item.get("reason", "")).lower():
                    return True
        except (ValueError, AttributeError):
            pass
    return "quota" in str(exc).lower()


class Uploader:
    def __init__(
        self,
        service,
        quota: QuotaTracker | None = None,
        playlist_id: str | None = None,
    ):
        self.service = service
        self.quota = quota or QuotaTracker()
        self.playlist_id = playlist_id

    def upload(
        self,
        media_path: str,
        title: str,
        description: str,
        privacy: str = "private",
        category_id: str = DEFAULT_CATEGORY_ID,
        progress_callback=None,
        max_retries: int = 5,
        base_delay: float = 2.0,
        sleep=time.sleep,
    ) -> str:
        """Upload a media file and return the new video ID."""
        if not self.quota.can_upload():
            _logger.warning(
                "upload refused: daily quota exhausted (%d/%d units used)",
                self.quota.used,
                self.quota.daily_limit,
            )
            raise QuotaExceededError(
                f"daily quota exhausted ({self.quota.used}/{self.quota.daily_limit} units used)"
            )

        size = os.path.getsize(media_path)
        _logger.info(
            "uploading %s (%.1f MB) as %r (privacy=%s, category=%s)",
            media_path,
            size / (1024 * 1024),
            title,
            privacy,
            category_id,
        )
        body = build_upload_body(title, description, privacy, category_id, made_for_kids=False)
        media = MediaFileUpload(media_path, chunksize=-1, resumable=True)
        request = self.service.videos().insert(part="snippet,status", body=body, media_body=media)

        response = self._execute_with_retry(
            request, max_retries=max_retries, base_delay=base_delay, sleep=sleep
        )
        self.quota.consume()

        video_id = response.get("id")
        _logger.info(
            "upload complete: video id %s (quota used %d/%d)",
            video_id,
            self.quota.used,
            self.quota.daily_limit,
        )
        if self.playlist_id:
            self.add_to_playlist(video_id)
        return video_id

    def _execute_with_retry(self, request, max_retries=5, base_delay=2.0, sleep=time.sleep):
        for attempt in range(max_retries + 1):
            try:
                return request.execute()
            except Exception as exc:  # noqa: BLE001 - retry transient errors
                if is_quota_error(exc):
                    _logger.warning("upload hit quota error: %s", exc)
                    raise QuotaExceededError(f"upload quota exceeded: {exc}") from exc
                if attempt == max_retries:
                    _logger.error("upload failed after %d attempt(s): %s", attempt + 1, exc)
                    raise
                delay = base_delay * (2 ** attempt)
                _logger.warning(
                    "upload attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                    delay,
                )
                sleep(delay)
        raise RuntimeError("retries exhausted")

    def add_to_playlist(self, video_id: str, playlist_id: str | None = None) -> dict:
        """Append a video to a playlist (segments are uploaded in time order)."""
        pid = playlist_id or self.playlist_id
        if not pid:
            return {}
        _logger.info("adding video %s to playlist %s", video_id, pid)
        request = self.service.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": pid,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        )
        return request.execute()

    def verify(self, video_id: str) -> None:
        """Verify an uploaded video exists; raise if it does not."""
        _logger.info("verifying uploaded video %s exists", video_id)
        response = self.service.videos().list(part="status", id=video_id).execute()
        if not response.get("items"):
            _logger.error("uploaded video %s not found during verification", video_id)
            raise RuntimeError(f"uploaded video {video_id} not found")
        _logger.debug("verified video %s", video_id)

    def delete_video(self, video_id: str) -> None:
        """Delete a video (used by rebuild). Requires the youtube.force-ssl scope."""
        _logger.info("deleting old video %s", video_id)
        self.service.videos().delete(id=video_id).execute()
        _logger.debug("deleted video %s", video_id)
