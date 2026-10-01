"""Segment rebuild orchestration.

A sealed segment is immutable on YouTube, so rebuilding follows a strict
order (per prompt.md): transcode and upload the new segment, verify it, then
delete the old video, then commit the new segment record (removing the old
record). Any failure before the delete keeps the old video intact.

The database is content-addressed: a rebuild produces a *new* ``segment_id``
that replaces the old record; the old ``youtube_video_id`` moves into the new
record's ``previous_video_ids``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .chapters import chapters_text
from .db import SEGMENT_STATUS_FAILED, SEGMENT_STATUS_SEALED, Database
from .log import get_logger
from .planner import Segment
from .scanner import utc_now_iso
from .uploader import QuotaExceededError, QuotaTracker

_logger = get_logger("rebuild")


@dataclass
class RebuildConfig:
    cooldown: float = 86400.0  # 24h: min interval between rebuilds of one segment
    quiet_period: float = 600.0  # 10min: wait after last change before rebuilding


def should_rebuild(
    last_rebuilt_ts: float | None,
    last_change_ts: float | None,
    now: float,
    cooldown: float,
    quiet_period: float,
) -> bool:
    """Debounce: reject rebuilds inside the cooldown or quiet period."""
    if last_rebuilt_ts is not None and now - last_rebuilt_ts < cooldown:
        return False
    if last_change_ts is not None and now - last_change_ts < quiet_period:
        return False
    return True


class Rebuilder:
    """Runs the atomic rebuild sequence.

    Injected callables:
      - transcode_fn(files) -> (output_path, chapters)
      - upload_fn(output_path, title, description) -> video_id
      - verify_fn(video_id) -> None (raises on failure)
      - delete_fn(video_id) -> None
    """

    def __init__(
        self,
        db: Database,
        transcode_fn: Callable,
        upload_fn: Callable,
        verify_fn: Callable,
        delete_fn: Callable,
        quota: QuotaTracker | None = None,
        config: RebuildConfig | None = None,
    ):
        self.db = db
        self.transcode_fn = transcode_fn
        self.upload_fn = upload_fn
        self.verify_fn = verify_fn
        self.delete_fn = delete_fn
        self.quota = quota or QuotaTracker()
        self.config = config or RebuildConfig()

    def rebuild(
        self,
        old_segment_id: str,
        new_segment: Segment,
        files: list,
        title: str = "",
        description: str | None = None,
    ) -> str:
        old = self.db.get_segment(old_segment_id)
        old_video_id = old.get("youtube_video_id") if old else None
        previous = list(old.get("previous_video_ids", [])) if old else []
        _logger.info(
            "rebuild start: %s -> %s (old video %s, %d file(s))",
            old_segment_id,
            new_segment.segment_id,
            old_video_id,
            len(files),
        )

        # Quota exhaustion defers to the next day; it is not a failure.
        if not self.quota.can_upload():
            _logger.warning(
                "rebuild deferred: quota exhausted (%d/%d)",
                self.quota.used,
                self.quota.daily_limit,
            )
            raise QuotaExceededError(
                f"quota exhausted ({self.quota.used}/{self.quota.daily_limit})"
            )

        try:
            _logger.debug("step 1/5: transcoding new segment")
            output_path, chapters = self.transcode_fn(files)  # 1. transcode new
            if description is None:
                description = chapters_text(chapters)
            _logger.debug("step 2/5: uploading new segment")
            new_video_id = self.upload_fn(output_path, title, description)  # 2. upload new
            _logger.debug("step 3/5: verifying new video %s", new_video_id)
            self.verify_fn(new_video_id)  # 3. verify chapters/duration
            if old_video_id:
                _logger.debug("step 4/5: deleting old video %s", old_video_id)
                self.delete_fn(old_video_id)  # 4. delete old (only now)
                previous = previous + [old_video_id]

            # 5. commit: new record replaces the old one.
            _logger.debug("step 5/5: committing new segment record")
            self.db.upsert_segment(
                new_segment.segment_id,
                {
                    "file_ids": new_segment.file_ids,
                    "range": [new_segment.start_ts, new_segment.end_ts],
                    "duration_seconds": new_segment.duration_seconds,
                    "output_path": output_path,
                    "youtube_video_id": new_video_id,
                    "previous_video_ids": previous,
                    "status": SEGMENT_STATUS_SEALED,
                    "chapters": chapters,
                    "last_rebuilt_at": utc_now_iso(),
                    "attempts": 0,
                    "error": None,
                },
            )
            self.db.segments.pop(old_segment_id, None)
            _logger.info(
                "rebuild committed: %s -> video %s (old %s deleted)",
                new_segment.segment_id,
                new_video_id,
                old_video_id,
            )
            return new_video_id
        except QuotaExceededError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep old video, record failure
            _logger.error(
                "rebuild failed for %s: %s (old video %s kept)",
                old_segment_id,
                exc,
                old_video_id,
            )
            self._fail(old_segment_id, str(exc))
            raise

    def _fail(self, segment_id: str, message: str) -> None:
        record = self.db.get_segment(segment_id)
        if record is not None:
            record["status"] = SEGMENT_STATUS_FAILED
            record["attempts"] = record.get("attempts", 0) + 1
            record["error"] = message
