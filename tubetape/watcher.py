"""File monitoring for watch mode.

``Watcher`` uses watchdog (inotify/FSEvents/...) to detect new media files
immediately. ``MtimeScanner`` is a cheap polling safety net (directory mtime)
for file systems where events are not delivered, e.g. network shares.
"""

from __future__ import annotations

import os
from typing import Callable

from .log import get_logger
from .scanner import MEDIA_EXTENSIONS

_logger = get_logger("watcher")


def is_media_path(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in MEDIA_EXTENSIONS


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
    """Watch ``input_dir`` for new media files using watchdog."""

    def __init__(
        self,
        input_dir: str,
        on_new_file: Callable[[str], None],
        use_watchdog: bool = True,
    ):
        self.input_dir = input_dir
        self.on_new_file = on_new_file
        self.use_watchdog = use_watchdog
        self._observer = None

    def start(self) -> None:
        if not self.use_watchdog:
            _logger.debug("watchdog disabled")
            return
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
