"""Parsing helpers for durations, resolutions, and timezones.

Formats (per prompt.md "启动参数"):
  - duration:  "20m" | "1200s" | "0:20:00" | "24h" | plain seconds ("1200")
  - resolution: "3840x2160"
  - timezone:   an IANA name such as "Asia/Shanghai"
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)([hms])$", re.IGNORECASE)
_UNIT_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0}

_RESOLUTION_RE = re.compile(r"^(\d+)\s*[xX]\s*(\d+)$")


def parse_duration(value: str) -> float:
    """Parse a duration string into seconds.

    Accepts a plain number of seconds ("1200"), a suffixed value ("20m",
    "1200s", "24h"), or clock notation ("0:20:00" = H:MM:SS, "20:00" = MM:SS).
    """
    text = str(value).strip()
    if not text:
        raise ValueError("duration must not be empty")

    match = _DURATION_RE.match(text)
    if match:
        number = float(match.group(1))
        seconds = number * _UNIT_SECONDS[match.group(2).lower()]
        if seconds < 0:
            raise ValueError(f"duration must not be negative: {value!r}")
        return seconds

    if ":" in text:
        parts = text.split(":")
        if not 2 <= len(parts) <= 3:
            raise ValueError(f"invalid duration: {value!r}")
        try:
            numbers = [float(part) for part in parts]
        except ValueError as exc:
            raise ValueError(f"invalid duration: {value!r}") from exc
        if any(n < 0 for n in numbers):
            raise ValueError(f"duration must not be negative: {value!r}")
        if len(parts) == 3:
            hours, minutes, seconds = numbers
        else:
            hours, minutes, seconds = 0.0, numbers[0], numbers[1]
        if minutes >= 60 or seconds >= 60:
            raise ValueError(
                f"invalid duration (minutes and seconds must be < 60): {value!r}"
            )
        return hours * 3600.0 + minutes * 60.0 + seconds

    try:
        seconds = float(text)
    except ValueError as exc:
        raise ValueError(f"invalid duration: {value!r}") from exc
    if seconds < 0:
        raise ValueError(f"duration must not be negative: {value!r}")
    return seconds


def parse_resolution(value: str) -> tuple[int, int]:
    """Parse a resolution string like "3840x2160" into (width, height)."""
    text = str(value).strip()
    match = _RESOLUTION_RE.match(text)
    if not match:
        raise ValueError(
            f"invalid resolution: {value!r} (expected WxH such as 3840x2160)"
        )
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        raise ValueError(f"resolution must be positive: {value!r}")
    return width, height


def system_local_timezone():
    """Return the system's local timezone as a tzinfo.

    Prefers the IANA name from /etc/timezone (Debian/Ubuntu) so that DST
    rules are preserved; falls back to the local fixed offset otherwise.
    """
    try:
        with open("/etc/timezone", encoding="utf-8") as handle:
            name = handle.read().strip()
        if name:
            return ZoneInfo(name)
    except (OSError, ZoneInfoNotFoundError):
        pass
    return datetime.now().astimezone().tzinfo


def parse_timezone(value: str):
    """Parse a timezone string into a tzinfo.

    Empty input falls back to the system local timezone.
    """
    text = str(value).strip() if value is not None else ""
    if not text:
        return system_local_timezone()
    try:
        return ZoneInfo(text)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {value!r}") from exc
