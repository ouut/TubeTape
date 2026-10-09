"""JSON database with atomic writes.

Schema (from prompt.md):

    {
      "version": 1,
      "config": {
        "segment_duration": 3600.0,
        "keep_segments": 0,
        "no_upload": false,
        "force_scan": false
      },
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
          "status": "sealed | failed",
          "chapters": [["0:00", "20240101-153000"]],
          "last_rebuilt_at": "..." | null,
          "attempts": 0,
          "error": null
        }
      }
    }
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any

from .log import get_logger

_logger = get_logger("db")

SCHEMA_VERSION = 1

SEGMENT_STATUS_SEALED = "sealed"
SEGMENT_STATUS_FAILED = "failed"

FILE_TYPE_IMAGE = "image"
FILE_TYPE_VIDEO = "video"


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
        config: dict[str, Any] | None = None,
        version: int = SCHEMA_VERSION,
        path: str | None = None,
    ):
        self.version = version
        self.config = config if config is not None else {}
        self.files = files if files is not None else {}
        self.segments = segments if segments is not None else {}
        self.path = path

    # ------------------------------------------------------------------ I/O

    @classmethod
    def load(cls, path: str) -> "Database":
        """Load a database file, or return an empty database if it is missing."""
        if not os.path.exists(path):
            _logger.info("database %s does not exist; starting with an empty one", path)
            cfg_file = os.path.join(os.path.dirname(os.path.abspath(path)), "config.json")
            legacy_config: dict[str, Any] = {}
            if os.path.isfile(cfg_file):
                try:
                    with open(cfg_file, "r", encoding="utf-8") as handle:
                        legacy_config = json.load(handle)
                    _logger.info("migrated legacy config from %s into database", cfg_file)
                except Exception as exc:
                    _logger.warning("could not read legacy %s: %s", cfg_file, exc)
            return cls(path=os.path.abspath(path), config=legacy_config)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except json.JSONDecodeError as exc:
            _logger.error("corrupt database %s: %s", path, exc)
            raise DatabaseError(f"corrupt database {path}: {exc}") from exc
        except OSError as exc:
            _logger.error("cannot read database %s: %s", path, exc)
            raise DatabaseError(f"cannot read database {path}: {exc}") from exc

        if not isinstance(raw, dict):
            _logger.error("database %s must contain a JSON object", path)
            raise DatabaseError(f"database {path} must contain a JSON object")

        config = raw.get("config")
        if config is None or not isinstance(config, dict):
            cfg_file = os.path.join(os.path.dirname(os.path.abspath(path)), "config.json")
            if os.path.isfile(cfg_file):
                try:
                    with open(cfg_file, "r", encoding="utf-8") as handle:
                        config = json.load(handle)
                    _logger.info("migrated legacy config from %s into database", cfg_file)
                except Exception as exc:
                    _logger.warning("could not read legacy %s: %s", cfg_file, exc)
                    config = {}
            else:
                config = {}

        _logger.info(
            "database loaded: %s (%d file(s), %d segment(s), %d config key(s))",
            path,
            len(raw.get("files", {})),
            len(raw.get("segments", {})),
            len(config),
        )

        return cls(
            version=raw.get("version", SCHEMA_VERSION),
            config=config,
            files=raw.get("files", {}),
            segments=raw.get("segments", {}),
            path=os.path.abspath(path),
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "config": self.config,
            "files": self.files,
            "segments": self.segments,
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
        _logger.debug(
            "database saved to %s (%d file(s), %d segment(s))",
            target,
            len(self.files),
            len(self.segments),
        )

    # ------------------------------------------------------------ file access

    def upsert_file(self, file_id: str, record: dict) -> None:
        _logger.debug("upsert file %s (%s)", file_id[:12], record.get("path"))
        self.files[file_id] = record

    def remove_file(self, file_id: str) -> None:
        if file_id in self.files:
            _logger.debug("remove file %s", file_id[:12])
        self.files.pop(file_id, None)

    # --------------------------------------------------------- segment access

    def get_segment(self, segment_id: str) -> dict | None:
        return self.segments.get(segment_id)

    def upsert_segment(self, segment_id: str, record: dict) -> None:
        _logger.debug(
            "upsert segment %s (status=%s, video=%s)",
            segment_id[:12],
            record.get("status"),
            record.get("youtube_video_id"),
        )
        self.segments[segment_id] = record
