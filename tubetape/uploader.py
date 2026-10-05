"""YouTube upload with retry and quota-error handling.

Uploads are resumable and retried with exponential backoff. A quota-exceeded
error is not retried: it is surfaced as :class:`QuotaExceededError` so the
caller can stop and back off until the quota resets.
"""

from __future__ import annotations

import json
import os
import time

from googleapiclient.http import MediaFileUpload

from .log import get_logger

_logger = get_logger("uploader")

DEFAULT_CATEGORY_ID = "22"  # People & Blogs


class QuotaExceededError(RuntimeError):
    """Raised when the YouTube upload quota is exhausted."""


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


DEFAULT_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB resumable chunks


class Uploader:
    def __init__(
        self,
        service,
        playlist_id: str | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ):
        self.service = service
        self.playlist_id = playlist_id
        self.chunk_size = chunk_size

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
        media = MediaFileUpload(media_path, chunksize=self.chunk_size, resumable=True)
        request = self.service.videos().insert(part="snippet,status", body=body, media_body=media)

        response = self._execute_resumable_upload(
            request,
            max_retries=max_retries,
            base_delay=base_delay,
            sleep=sleep,
            progress_callback=progress_callback,
        )

        video_id = response.get("id")
        _logger.info("upload complete: video id %s", video_id)
        if self.playlist_id:
            self.add_to_playlist(video_id)
        return video_id

    def _execute_resumable_upload(
        self,
        request,
        max_retries: int = 5,
        base_delay: float = 2.0,
        sleep=time.sleep,
        progress_callback=None,
    ):
        if hasattr(request, "next_chunk"):
            response = None
            retry = 0
            while response is None:
                try:
                    status, response = request.next_chunk()
                    if status and progress_callback:
                        progress_callback(status.progress())
                    retry = 0
                except Exception as exc:
                    if is_quota_error(exc):
                        _logger.warning("upload hit quota error: %s", exc)
                        raise QuotaExceededError(f"upload quota exceeded: {exc}") from exc
                    retry += 1
                    if retry > max_retries:
                        _logger.error("resumable upload failed after %d attempt(s): %s", retry, exc)
                        raise
                    delay = base_delay * (2 ** (retry - 1))
                    _logger.warning(
                        "upload chunk failed (%s); retrying in %.1fs (attempt %d/%d)",
                        exc,
                        delay,
                        retry,
                        max_retries,
                    )
                    sleep(delay)
            return response
        return self._execute_with_retry(request, max_retries=max_retries, base_delay=base_delay, sleep=sleep)

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
        try:
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
        except Exception as exc:  # noqa: BLE001 - playlist errors should not abort upload success
            _logger.warning("could not add video %s to playlist %s: %s (continuing)", video_id, pid, exc)
            return {}

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
