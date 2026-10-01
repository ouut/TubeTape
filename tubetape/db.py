"""JSON database with atomic writes.

Schema (from prompt.md):

    {
      "version": 1,
      "files": {
        "<file_id>": {
          "path": "relative path",
          "name": "file name",
          "type": "image | video",
          "captured_at_utc": "2024-01-01T15:30:00Z",
          "resolution": "4000x3000",
          "duration_seconds": 3.0,
          "size_bytes": 1234567,
          "sha256": "...",
          "location": {"lat": 0.0, "lng": 0.0} | null,
          "missing_meta": false
        }
      },
      "segments": {
        "<segment_id>": {
          "file_ids": ["..."],
          "range": ["2024-01-01T15:30:00Z", "2024-01-01T15:40:00Z"],
          "duration_seconds": 600.0,
          "output_path": "...",
          "youtube_video_id": "..." | null,
          "previous_video_ids": ["..."],
          "status": "pending | encoding | uploading | sealed | rebuild_pending | failed | skipped",
          "chapters": [["0:00", "20240101-153000"]],
          "last_rebuilt_at": "..." | null,
          "attempts": 0,
          "error": null
        }
      },
      "queue": {"pending_file_ids": ["..."], "rebuild_segment_ids": ["..."]},
      "errors": [{"path": "...", "reason": "...", "ts": "..."}],
      "settings": {}
    }
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

SCHEMA_VERSION = 1

SEGMENT_STATUS_PENDING = "pending"
SEGMENT_STATUS_ENCODING = "encoding"
SEGMENT_STATUS_UPLOADING = "uploading"
SEGMENT_STATUS_SEALED = "sealed"
SEGMENT_STATUS_REBUILD_PENDING = "rebuild_pending"
SEGMENT_STATUS_FAILED = "failed"
SEGMENT_STATUS_SKIPPED = "skipped"

SEGMENT_STATUSES = frozenset(
    [
        SEGMENT_STATUS_PENDING,
        SEGMENT_STATUS_ENCODING,
        SEGMENT_STATUS_UPLOADING,
        SEGMENT_STATUS_SEALED,
        SEGMENT_STATUS_REBUILD_PENDING,
        SEGMENT_STATUS_FAILED,
        SEGMENT_STATUS_SKIPPED,
    ]
)

FILE_TYPE_IMAGE = "image"
FILE_TYPE_VIDEO = "video"
FILE_TYPES = frozenset([FILE_TYPE_IMAGE, FILE_TYPE_VIDEO])

DEFAULT_QUEUE = {"pending_file_ids": [], "rebuild_segment_ids": []}


class DatabaseError(Exception):
    """Raised when the database cannot be loaded or saved."""


class Database:
    """The tubetape.json database.

    Writes are atomic: the payload is written to a temp file in the same
    directory and then moved into place with ``os.replace``, so a crash
    mid-write never corrupts the existing database.
    """

    def __init__(
        self,
        *,
        files: dict[str, dict] | None = None,
        segments: dict[str, dict] | None = None,
        queue: dict | None = None,
        errors: list | None = None,
        settings: dict | None = None,
        version: int = SCHEMA_VERSION,
        path: str | None = None,
    ):
        self.version = version
        self.files = files if files is not None else {}
        self.segments = segments if segments is not None else {}
        self.queue = (
            queue
            if queue is not None
            else {"pending_file_ids": [], "rebuild_segment_ids": []}
        )
        self.errors = errors if errors is not None else []
        self.settings = settings if settings is not None else {}
        self.path = path

    # ------------------------------------------------------------------ I/O

    @classmethod
    def load(cls, path: str) -> "Database":
        """Load a database file, or return an empty database if it is missing."""
        if not os.path.exists(path):
            return cls(path=os.path.abspath(path))
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except json.JSONDecodeError as exc:
            raise DatabaseError(f"corrupt database {path}: {exc}") from exc
        except OSError as exc:
            raise DatabaseError(f"cannot read database {path}: {exc}") from exc

        if not isinstance(raw, dict):
            raise DatabaseError(f"database {path} must contain a JSON object")

        queue = raw.get("queue", {})
        if not isinstance(queue, dict):
            queue = {}
        queue.setdefault("pending_file_ids", [])
        queue.setdefault("rebuild_segment_ids", [])

        return cls(
            version=raw.get("version", SCHEMA_VERSION),
            files=raw.get("files", {}),
            segments=raw.get("segments", {}),
            queue=queue,
            errors=raw.get("errors", []),
            settings=raw.get("settings", {}),
            path=os.path.abspath(path),
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "files": self.files,
            "segments": self.segments,
            "queue": self.queue,
            "errors": self.errors,
            "settings": self.settings,
        }

    def save(self, path: str | None = None) -> None:
        """Atomically persist the database to ``path`` (or ``self.path``)."""
        target = path if path is not None else self.path
        if target is None:
            raise DatabaseError("no database path specified")
        target = os.path.abspath(target)
        directory = os.path.dirname(target) or "."
        os.makedirs(directory, exist_ok=True)

        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tubetape-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, target)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        self.path = target

    # ------------------------------------------------------------ file access

    def get_file(self, file_id: str) -> dict | None:
        return self.files.get(file_id)

    def upsert_file(self, file_id: str, record: dict) -> None:
        self.files[file_id] = record

    def remove_file(self, file_id: str) -> None:
        self.files.pop(file_id, None)

    # --------------------------------------------------------- segment access

    def get_segment(self, segment_id: str) -> dict | None:
        return self.segments.get(segment_id)

    def upsert_segment(self, segment_id: str, record: dict) -> None:
        self.segments[segment_id] = record

    def set_segment_status(self, segment_id: str, status: str) -> None:
        if status not in SEGMENT_STATUSES:
            raise ValueError(f"unknown segment status: {status!r}")
        segment = self.segments.get(segment_id)
        if segment is None:
            raise KeyError(f"unknown segment: {segment_id!r}")
        segment["status"] = status

    # ---------------------------------------------------------------- errors

    def add_error(self, path: str, reason: str, ts: str | None = None) -> None:
        if ts is None:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.errors.append({"path": path, "reason": reason, "ts": ts})

    # ----------------------------------------------------------------- queue

    def enqueue_pending_file(self, file_id: str) -> None:
        if file_id not in self.queue["pending_file_ids"]:
            self.queue["pending_file_ids"].append(file_id)

    def enqueue_rebuild(self, segment_id: str) -> None:
        if segment_id not in self.queue["rebuild_segment_ids"]:
            self.queue["rebuild_segment_ids"].append(segment_id)
