"""File monitoring and flush triggers.

Watchdog observes new files (with a polling fallback). New files are
classified by capture time against existing segment ranges: inside a range
means "rebuild that segment", outside means "queue for a new segment".
"""

from __future__ import annotations

import os
from typing import Callable

from .scanner import MEDIA_EXTENSIONS


def is_media_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in MEDIA_EXTENSIONS


def classify_capture_time(epoch: float | None, ranges: list[tuple[float, float]]) -> str:
    """Return "inside" if ``epoch`` falls in any inclusive [start, end] range."""
    if epoch is None:
        return "outside"
    for start, end in ranges:
        if start <= epoch <= end:
            return "inside"
    return "outside"


def should_flush(flush_requested: bool, idle_seconds: float, idle_timeout: float) -> bool:
    """Flush when requested, or when the queue has been idle past the timeout."""
    return flush_requested or idle_seconds >= idle_timeout


class _MediaFileHandler:
    def __init__(self, callback: Callable[[str], None]):
        self.callback = callback

    def on_created(self, event) -> None:
        if not getattr(event, "is_directory", False) and is_media_path(event.src_path):
            self.callback(event.src_path)


class Watcher:
    """Watch ``input_dir`` for new media files using watchdog (or polling)."""

    def __init__(
        self,
        input_dir: str,
        on_new_file: Callable[[str], None],
        use_watchdog: bool = True,
        poll_interval: float = 10.0,
    ):
        self.input_dir = input_dir
        self.on_new_file = on_new_file
        self.use_watchdog = use_watchdog
        self.poll_interval = poll_interval
        self._observer = None

    def start(self) -> None:
        if not self.use_watchdog:
            return
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        handler = _MediaFileHandler(self.on_new_file)
        self._observer = Observer()
        self._observer.schedule(handler, self.input_dir, recursive=True)
        self._observer.start()

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def poll_once(self, known_paths: set[str]) -> set[str]:
        """Polling fallback: return media paths not present in ``known_paths``."""
        found: set[str] = set()
        for root, _dirs, names in os.walk(self.input_dir):
            for name in names:
                path = os.path.join(root, name)
                if is_media_path(path):
                    found.add(path)
        return found - known_paths
