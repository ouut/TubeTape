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


def build_segment_description(files, total_duration: float | None = None) -> str:
    """Build a concise statistical YouTube description from segment files.

    Format:
    照片: N 张
    视频: M 条
    总时长: M:SS (或 H:MM:SS)

    视频明细 (按构建顺序):
    1. M:SS
    2. M:SS
    """
    photos = []
    videos = []
    computed_duration = 0.0

    for f in files:
        dur = getattr(f, "duration_seconds", 0.0) or 0.0
        computed_duration += dur
        ftype = getattr(f, "type", None)
        if ftype == "video":
            videos.append(f)
        else:
            photos.append(f)

    if total_duration is None:
        total_duration = computed_duration

    lines = [
        f"照片: {len(photos)} 张",
        f"视频: {len(videos)} 条",
        f"总时长: {format_chapter_timestamp(total_duration)}",
    ]

    if videos:
        lines.append("")
        lines.append("视频明细 (按构建顺序):")
        for idx, vid in enumerate(videos, 1):
            dur = getattr(vid, "duration_seconds", 0.0) or 0.0
            lines.append(f"{idx}. {format_chapter_timestamp(dur)}")

    return "\n".join(lines)


def chapters_text(chapters: list, max_len: int = 4800) -> str:
    """Render chapters or file statistics as YouTube description text.

    If a list of ScannedFile is passed, returns statistical description.
    Guarantees the text does not exceed max_len (YouTube description limit is 5000 chars).
    """
    if chapters and hasattr(chapters[0], "duration_seconds"):
        return build_segment_description(chapters)

    lines = [f"{item[0]} {item[1]}" for item in chapters]
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

