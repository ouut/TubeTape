"""Progress reporting.

Uses ``rich`` when stdout is a TTY; falls back to plain, redirectable log
lines otherwise (so output is clean under ``--dry-run`` or CI).
"""

from __future__ import annotations

import sys

from .log import get_logger

_logger = get_logger("ui")


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
        # Mirror every user-facing status line into the detailed log as well,
        # so the log file tells the whole story end to end.
        _logger.info(message)
        try:
            from .web import set_web_status
            set_web_status(message)
        except Exception:  # noqa: BLE001
            pass
        if self._console is not None:
            self._console.print(message)
        else:
            print(message)

    def warning(self, message: str) -> None:
        _logger.warning(message)
        self.status(f"warning: {message}")

    def error(self, message: str) -> None:
        _logger.error(message)
        self.status(f"error: {message}")

    def debug(self, message: str) -> None:
        _logger.debug(message)

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

    def upload_progress(
        self,
        title: str,
        percent: float,
        uploaded_bytes: int = 0,
        total_bytes: int = 0,
        speed: str = "",
    ) -> None:
        width = 24
        pct = max(0.0, min(100.0, percent))
        filled = int(round(width * (pct / 100.0)))
        filled = max(0, min(width, filled))
        bar = "━" * filled + ("╸" if filled < width else "")
        if len(bar) < width:
            bar += "━" * (width - len(bar))

        up_mb = uploaded_bytes / (1024 * 1024)
        tot_mb = total_bytes / (1024 * 1024)
        size_str = f"({up_mb:.1f} MB / {tot_mb:.1f} MB)" if total_bytes > 0 else ""
        spd_str = f"| {speed}" if speed and speed != "N/A" else ""

        msg = f"[上传中] {title} {bar} {pct:.1f}% {size_str} {spd_str}".strip()
        _logger.info(msg)
        try:
            from .web import set_web_status
            set_web_status("uploading", f"正在上传 {title} ({pct:.1f}%) {spd_str}".strip())
        except Exception:
            pass

        if self._console is not None:
            self._console.print(
                f"[bold cyan][上传中][/bold cyan] [white]{title}[/white] "
                f"[green]{bar}[/green] [bold yellow]{pct:.1f}%[/bold yellow] "
                f"[dim]{size_str}[/dim] [bold magenta]{spd_str}[/bold magenta]"
            )
        else:
            print(msg)


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
