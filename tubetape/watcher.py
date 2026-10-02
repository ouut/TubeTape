"""File monitoring and flush triggers.

Watchdog observes new files (with a polling fallback). New files are
classified by capture time against existing segment ranges: inside a range
means "rebuild that segment", outside means "queue for a new segment".
"""

from __future__ import annotations

import os
from typing import Callable

from .log import get_logger
from .scanner import MEDIA_EXTENSIONS

_logger = get_logger("watcher")


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

    def on_moved(self, event) -> None:
        dest = getattr(event, "dest_path", None)
        if dest and not getattr(event, "is_directory", False) and is_media_path(dest):
            self.callback(dest)


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
            _logger.debug("watchdog disabled; using polling only")
            return
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        handler = _MediaFileHandler(self.on_new_file)
        self._observer = Observer()
        self._observer.schedule(handler, self.input_dir, recursive=True)
        self._observer.start()
        _logger.info("watchdog observer started on %s", self.input_dir)

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
            _logger.debug("watchdog observer stopped")

    def poll_once(self, known_paths: set[str]) -> set[str]:
        """Polling fallback: return media paths not present in ``known_paths``."""
        found: set[str] = set()
        for root, _dirs, names in os.walk(self.input_dir):
            for name in names:
                path = os.path.join(root, name)
                if is_media_path(path):
                    found.add(path)
        new = found - known_paths
        if new:
            _logger.debug("poll_once found %d new media path(s)", len(new))
        return new


class MtimeScanner:
    """Detect changed directories cheaply via directory mtime.

    A directory's mtime changes only when an entry inside it is created,
    deleted or renamed (not when a file's content is modified). Caching each
    directory's mtime and child list means a steady-state scan only ``stat``s
    directories and re-lists the few that changed, so its cost is
    O(#directories) instead of O(#files).

    This is the polling safety net for file systems where inotify/watchdog
    does not deliver events (e.g. network shares).
    """

    def __init__(self, root: str):
        self.root = root
        self._dirs: dict[str, tuple[int, list[str]]] = {}

    def scan(self) -> list[str]:
        """Return directories whose contents changed since the last call."""
        changed: list[str] = []
        self._walk(self.root, changed)
        return changed

    def _walk(self, path: str, changed: list[str]) -> None:
        try:
            mtime_ns = os.stat(path).st_mtime_ns
        except OSError:
            self._dirs.pop(path, None)
            return

        cached = self._dirs.get(path)
        if cached is not None and cached[0] == mtime_ns:
            # Unchanged: the set of child directories is unchanged too, so
            # reuse it instead of re-listing this directory.
            for child in cached[1]:
                self._walk(child, changed)
            return

        children: list[str] = []
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        children.append(entry.path)
        except OSError:
            pass
        self._dirs[path] = (mtime_ns, children)
        changed.append(path)
        for child in children:
            self._walk(child, changed)
