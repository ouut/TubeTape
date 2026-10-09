"""Segment planning.

Builds segments by sorting files by capture time and greedily packing them up
to ``segment_duration`` (compact concatenation, no time gaps filled). On later
runs, existing segment time ranges are fixed boundaries: files inside a range
are merged into that segment (rebuild), files outside are packed into new
segments.

``segment_id = sha256(sorted(file_ids) + segment params)`` — content or params
changes produce a new ID, which is what triggers re-transcode/re-upload.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .log import get_logger
from .scanner import ScannedFile, format_iso_utc, parse_iso_utc

_logger = get_logger("planner")

_DISPLAY_TS_FORMAT = "%Y%m%d-%H%M%S"


def display_ts(epoch: float) -> str:
    """Format an epoch as ``YYYYMMDD-HHMMSS`` (UTC)."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(_DISPLAY_TS_FORMAT)


def segment_id(file_ids: list[str], params: tuple) -> str:
    """Compute a segment's stable ID from its sorted file IDs and params."""
    hasher = hashlib.sha256()
    for file_id in sorted(file_ids):
        hasher.update(file_id.encode("utf-8"))
        hasher.update(b"\x00")
    for param in params:
        hasher.update(repr(param).encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


def _sort_key(f: ScannedFile):
    # Missing capture time sorts first, tie-break on file_id for determinism.
    has_epoch = 1 if f.captured_epoch is not None else 0
    epoch = f.captured_epoch if f.captured_epoch is not None else 0.0
    return (has_epoch, epoch, f.file_id)


@dataclass
class Segment:
    file_ids: list[str]
    start_ts: str
    end_ts: str
    duration_seconds: float
    segment_id: str
    title: str
    replaces_segment_id: str | None = None  # set when this rebuilds an old segment

    @property
    def is_rebuild(self) -> bool:
        return self.replaces_segment_id is not None


@dataclass
class Plan:
    segments: list[Segment] = field(default_factory=list)  # to transcode + upload
    skipped_segment_ids: list[str] = field(default_factory=list)
    pending_files: list[ScannedFile] = field(default_factory=list)  # held for flush


def _make_segment(
    files: list[ScannedFile],
    params: tuple,
    replaces_segment_id: str | None = None,
) -> Segment:
    if not files:
        raise ValueError("cannot build a segment from no files")
    ordered = sorted(files, key=_sort_key)
    epochs = [f.captured_epoch for f in ordered if f.captured_epoch is not None]
    sid = segment_id([f.file_id for f in ordered], params)
    short_id = sid[:16]

    if not epochs:
        # All files in this segment are undated: 19700101_000000-19700101_000000_[{short_id}]
        start_ts = "1970-01-01T00:00:00Z"
        end_ts = "1970-01-01T00:00:00Z"
        title = f"19700101_000000-19700101_000000_[{short_id}]"
    else:
        start = min(epochs)
        end = max(epochs)
        start_ts = format_iso_utc(datetime.fromtimestamp(start, tz=timezone.utc))
        end_ts = format_iso_utc(datetime.fromtimestamp(end, tz=timezone.utc))
        title = f"{display_ts(start)} - {display_ts(end)} [{short_id}]"

    return Segment(
        file_ids=[f.file_id for f in ordered],
        start_ts=start_ts,
        end_ts=end_ts,
        duration_seconds=sum(f.duration_seconds or 0.0 for f in ordered),
        segment_id=sid,
        title=title,
        replaces_segment_id=replaces_segment_id,
    )


def greedy_pack(files: list[ScannedFile], segment_duration: float) -> list[list[ScannedFile]]:
    """Greedily pack capture-time-sorted files into segments.

    Each segment accumulates content duration up to ``segment_duration``. A
    single file longer than the limit is never split: it closes the current
    segment and becomes its own (oversized) segment.
    """
    ordered = sorted(files, key=_sort_key)
    segments: list[list[ScannedFile]] = []
    current: list[ScannedFile] = []
    current_duration = 0.0

    for item in ordered:
        duration = item.duration_seconds or 0.0
        if duration > segment_duration:
            if current:
                segments.append(current)
                current = []
                current_duration = 0.0
            segments.append([item])
            continue
        if current and current_duration + duration > segment_duration:
            segments.append(current)
            current = []
            current_duration = 0.0
        current.append(item)
        current_duration += duration

    if current:
        segments.append(current)
    return segments


def _is_partial(group: list[ScannedFile], segment_duration: float) -> bool:
    duration = sum(f.duration_seconds or 0.0 for f in group)
    if duration >= segment_duration:
        return False
    # A single oversized file is complete, not partial.
    if len(group) == 1 and (group[0].duration_seconds or 0.0) > segment_duration:
        return False
    return True


def plan(
    files: list[ScannedFile],
    existing_segments: dict[str, dict],
    segment_duration: float,
    params: tuple = (),
    flush: bool = False,
) -> Plan:
    """Compute the segment plan.

    Existing segments are strictly IMMUTABLE: files already belonging to an
    existing segment stay in that segment, and existing segments are never modified,
    rebuilt, or split.

    New files:
    - Files with captured_epoch >= max_sealed_epoch (or all files if no sealed segments exist)
      are 'tail' files: greedily packed up to segment_duration, with the trailing partial group
      held as pending (unless flush=True).
    - Other files (historical files with captured_epoch < max_sealed_epoch, or undated files)
      are packed into new independent segments and sealed immediately.
    """
    result = Plan()
    known_ids = set(existing_segments.keys())
    existing_file_ids: set[str] = set()
    max_sealed_epoch: float | None = None

    _logger.info(
        "planning %d file(s) against %d existing segment(s)",
        len(files),
        len(existing_segments),
    )

    for sid, record in existing_segments.items():
        result.skipped_segment_ids.append(sid)
        for fid in record.get("file_ids", []):
            existing_file_ids.add(fid)

        rng = record.get("range")
        if isinstance(rng, list) and len(rng) == 2:
            end_dt = parse_iso_utc(rng[1])
            if end_dt is not None:
                ts = end_dt.timestamp()
                # Ignore epoch <= 0 (e.g. 1970 undated segments) when finding max_sealed_epoch
                if ts > 0:
                    if max_sealed_epoch is None or ts > max_sealed_epoch:
                        max_sealed_epoch = ts

    unassigned = [f for f in files if f.file_id not in existing_file_ids]
    if not unassigned:
        return result

    if max_sealed_epoch is None:
        # First run / no existing sealed segments with timestamps: pack everything
        ordered = sorted(unassigned, key=_sort_key)
        groups = greedy_pack(ordered, segment_duration)
        if groups and not flush and _is_partial(groups[-1], segment_duration):
            result.pending_files = groups.pop()
            _logger.info(
                "holding %d file(s) as pending (last group shorter than %.1fs and not flushing)",
                len(result.pending_files),
                segment_duration,
            )

        for group in groups:
            seg = _make_segment(group, params)
            if seg.segment_id not in known_ids:
                known_ids.add(seg.segment_id)
                result.segments.append(seg)
                _logger.info(
                    "new segment planned: %s (%s) with %d file(s), %.1fs",
                    seg.segment_id[:12],
                    seg.title,
                    len(seg.file_ids),
                    seg.duration_seconds,
                )
        return result

    # Later runs: split unassigned into tail and history files
    tail_files: list[ScannedFile] = []
    history_files: list[ScannedFile] = []

    for item in unassigned:
        if item.captured_epoch is not None and item.captured_epoch >= max_sealed_epoch:
            tail_files.append(item)
        else:
            history_files.append(item)

    # 1. History files (old or undated photos added after segments already exist)
    if history_files:
        history_ordered = sorted(history_files, key=_sort_key)
        history_groups = greedy_pack(history_ordered, segment_duration)
        for group in history_groups:
            seg = _make_segment(group, params)
            if seg.segment_id not in known_ids:
                known_ids.add(seg.segment_id)
                result.segments.append(seg)
                _logger.info(
                    "new historical segment planned: %s (%s) with %d file(s), %.1fs",
                    seg.segment_id[:12],
                    seg.title,
                    len(seg.file_ids),
                    seg.duration_seconds,
                )

    # 2. Tail files (newest photos >= max_sealed_epoch)
    if tail_files:
        tail_ordered = sorted(tail_files, key=_sort_key)
        tail_groups = greedy_pack(tail_ordered, segment_duration)
        if tail_groups and not flush and _is_partial(tail_groups[-1], segment_duration):
            result.pending_files = tail_groups.pop()
            _logger.info(
                "holding %d tail file(s) as pending (last group shorter than %.1fs and not flushing)",
                len(result.pending_files),
                segment_duration,
            )
        for group in tail_groups:
            seg = _make_segment(group, params)
            if seg.segment_id not in known_ids:
                known_ids.add(seg.segment_id)
                result.segments.append(seg)
                _logger.info(
                    "new tail segment planned: %s (%s) with %d file(s), %.1fs",
                    seg.segment_id[:12],
                    seg.title,
                    len(seg.file_ids),
                    seg.duration_seconds,
                )

    return result
