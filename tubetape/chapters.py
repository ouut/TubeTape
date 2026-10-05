"""Video chapter computation.

Each file becomes a chapter titled with its timestamp. The first chapter
starts at ``0:00``. Adjacent chapters must be at least ``min_gap`` seconds
apart (YouTube's constraint is 10s), so shorter adjacent files are merged into
one chapter.
"""

from __future__ import annotations

from .log import get_logger

_logger = get_logger("chapters")

DEFAULT_MIN_GAP = 10.0


def format_chapter_timestamp(seconds: float) -> str:
    """Format seconds as ``M:SS`` (or ``H:MM:SS`` when >= 1 hour)."""
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def build_chapters(items, min_gap: float = DEFAULT_MIN_GAP) -> list[list[str]]:
    """Build ``[[start, title], ...]`` chapters from ``(duration, title)`` items.

    A file starts a new chapter only when its start time is at least
    ``min_gap`` seconds after the current chapter's start; otherwise it is
    merged into the current chapter (keeping the first file's title).
    """
    chapters: list[list[str]] = []
    elapsed = 0.0
    chapter_start: float | None = None

    for duration, title in items:
        if chapter_start is None:
            chapter_start = elapsed
            chapters.append([format_chapter_timestamp(chapter_start), str(title)])
        elif elapsed - chapter_start >= min_gap:
            chapter_start = elapsed
            chapters.append([format_chapter_timestamp(chapter_start), str(title)])
        # else: merge into the current chapter (no new entry)
        elapsed += float(duration)

    _logger.debug("built %d chapter(s) from %d item(s)", len(chapters), len(items))
    return chapters


def chapters_text(chapters: list[list[str]], max_len: int = 4800) -> str:
    """Render chapters as YouTube description text.

    Guarantees the text does not exceed max_len (YouTube description limit is 5000 chars).
    """
    lines = [f"{start} {title}" for start, title in chapters]
    text = "\n".join(lines)
    if len(text) <= max_len:
        return text
    truncated_lines: list[str] = []
    total = 0
    for line in lines:
        if total + len(line) + 1 > max_len:
            break
        truncated_lines.append(line)
        total += len(line) + 1
    return "\n".join(truncated_lines)
