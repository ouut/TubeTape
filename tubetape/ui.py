"""Progress reporting.

Uses ``rich`` when stdout is a TTY; falls back to plain, redirectable log
lines otherwise (so output is clean under ``--dry-run`` or CI).
"""

from __future__ import annotations

import sys


class Reporter:
    def __init__(self, tty: bool | None = None):
        self.tty = sys.stdout.isatty() if tty is None else tty
        self._console = None
        if self.tty:
            try:
                from rich.console import Console

                self._console = Console()
            except ImportError:
                self._console = None

    def status(self, message: str) -> None:
        if self._console is not None:
            self._console.print(message)
        else:
            print(message)

    def task(self, name: str):
        """Context manager marking a task (rich live display in TTY mode)."""
        if self._console is None:
            self.status(f"task: {name}")
            return _NullContext()
        return self._console.status(name)

    def progress(self, current: int, total: int, label: str = "") -> None:
        if total <= 0:
            self.status(f"{label} {current}/?")
            return
        pct = current / total * 100
        self.status(f"{label} {current}/{total} ({pct:.0f}%)")

    def quota(self, used: int, limit: int) -> None:
        self.status(f"quota: {used}/{limit} units ({limit - used} remaining)")


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
