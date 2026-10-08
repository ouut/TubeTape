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
    # Missing capture time sorts last; tie-break on file_id for determinism.
    epoch = f.captured_epoch if f.captured_epoch is not None else float("inf")
    return (epoch, f.file_id)


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
    if not epochs:
        raise ValueError("segment files have no capture time")

    start = min(epochs)
    end = max(epochs)
    sid = segment_id([f.file_id for f in ordered], params)
    short_id = sid[:16]
    return Segment(
        file_ids=[f.file_id for f in ordered],
        start_ts=format_iso_utc(datetime.fromtimestamp(start, tz=timezone.utc)),
        end_ts=format_iso_utc(datetime.fromtimestamp(end, tz=timezone.utc)),
        duration_seconds=sum(f.duration_seconds or 0.0 for f in ordered),
        segment_id=sid,
        title=f"{display_ts(start)} - {display_ts(end)} [{short_id}]",
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

    ``existing_segments`` maps ``segment_id -> record`` (with ``file_ids`` and
    ``range``). Files inside an existing range are merged into that segment;
    files outside all ranges are packed into new segments.
    """
    files_by_id = {f.file_id: f for f in files}
    known_ids = set(existing_segments.keys())
    _logger.info(
        "planning %d file(s) against %d existing segment(s)",
        len(files),
        len(existing_segments),
    )

    # Parse existing ranges into epoch bounds, sorted and stable.
    ranges: list[tuple[float, float, str, dict]] = []
    for sid, record in existing_segments.items():
        rng = record.get("range")
        if not isinstance(rng, list) or len(rng) != 2:
            continue
        start_dt = parse_iso_utc(rng[0])
        end_dt = parse_iso_utc(rng[1])
        if start_dt is None or end_dt is None:
            continue
        ranges.append((start_dt.timestamp(), end_dt.timestamp(), sid, record))
    ranges.sort(key=lambda r: (r[0], r[1], r[2]))
    _logger.debug("parsed %d existing segment time range(s)", len(ranges))

    # Build a reverse lookup of files already belonging to existing segments.
    # Files already in a segment remain with that segment and cannot be stolen
    # by an adjacent segment with the same boundary timestamp.
    existing_owner: dict[str, str] = {}
    for sid, record in existing_segments.items():
        for fid in record.get("file_ids", []):
            existing_owner[fid] = sid

    # Calculate current duration and remaining capacity for each existing segment
    remaining_capacity: dict[str, float] = {}
    for _, _, sid, record in ranges:
        existing_dur = sum(
            files_by_id[fid].duration_seconds or 0.0
            for fid in record.get("file_ids", [])
            if fid in files_by_id
        )
        remaining_capacity[sid] = max(0.0, segment_duration - existing_dur)

    # Assign each file to its existing segment, or to a matching range for new files.
    assigned: dict[str, list[str]] = {sid: [] for _, _, sid, _ in ranges}
    free_files: list[ScannedFile] = []
    for item in files:
        if item.file_id in existing_owner:
            owner_sid = existing_owner[item.file_id]
            if owner_sid in assigned:
                assigned[owner_sid].append(item.file_id)
            continue

        if item.captured_epoch is None:
            free_files.append(item)
            continue

        # For new files, find candidate ranges that enclose item.captured_epoch.
        # Sort candidates by span (end - start) so tighter, more specific intervals take precedence.
        item_dur = item.duration_seconds or 0.0
        candidates = []
        for start, end, sid, _ in ranges:
            if start <= item.captured_epoch <= end:
                candidates.append((end - start, sid))
        candidates.sort(key=lambda x: x[0])

        placed = False
        for _, sid in candidates:
            if item_dur <= remaining_capacity[sid]:
                assigned[sid].append(item.file_id)
                remaining_capacity[sid] -= item_dur
                placed = True
                break

        if not placed:
            free_files.append(item)

    result = Plan()

    # Rebuild existing segments whose content changed.
    for _start, _end, sid, record in ranges:
        combined: list[ScannedFile] = []
        seen: set[str] = set()
        for file_id in list(record.get("file_ids", [])) + assigned[sid]:
            if file_id in seen or file_id not in files_by_id:
                continue
            seen.add(file_id)
            combined.append(files_by_id[file_id])

        if not combined:
            # Every file in this segment was deleted; nothing to rebuild now.
            _logger.debug("segment %s has no remaining files; skipping rebuild", sid)
            continue

        combined_dur = sum(f.duration_seconds or 0.0 for f in combined)
        # Ensure that no combined segment exceeds segment_duration unless a single file is oversized.
        if len(combined) > 1 and combined_dur > segment_duration:
            combined_groups = greedy_pack(combined, segment_duration)
        else:
            combined_groups = [combined]

        # First group replaces the existing segment (if changed)
        first_group = combined_groups[0]
        segment = _make_segment(first_group, params, replaces_segment_id=sid)
        if segment.segment_id == sid:
            result.skipped_segment_ids.append(sid)
            _logger.debug("segment %s unchanged; skipping", sid)
        elif segment.segment_id in known_ids:
            result.skipped_segment_ids.append(sid)
            _logger.debug("segment %s already known as %s; skipping", sid, segment.segment_id)
        else:
            known_ids.add(segment.segment_id)
            result.segments.append(segment)
            _logger.info(
                "rebuild planned: %s (%s) replaces %s",
                segment.segment_id[:12],
                segment.title,
                sid[:12],
            )

        # Any extra groups (if repacked) become new segments
        for extra_group in combined_groups[1:]:
            extra_seg = _make_segment(extra_group, params)
            if extra_seg.segment_id not in known_ids:
                known_ids.add(extra_seg.segment_id)
                result.segments.append(extra_seg)
                _logger.info(
                    "rebuild overflow segment planned: %s (%s) with %d file(s), %.1fs",
                    extra_seg.segment_id[:12],
                    extra_seg.title,
                    len(extra_seg.file_ids),
                    extra_seg.duration_seconds,
                )

    # Pack free files into new segments.
    groups = greedy_pack(free_files, segment_duration)
    _logger.debug("packed %d free file(s) into %d new group(s)", len(free_files), len(groups))
    if groups and not flush and _is_partial(groups[-1], segment_duration):
        result.pending_files = groups.pop()
        _logger.info(
            "holding %d file(s) as pending (last group shorter than %.1fs and not flushing)",
            len(result.pending_files),
            segment_duration,
        )

    for group in groups:
        segment = _make_segment(group, params)
        if segment.segment_id in known_ids:
            _logger.debug("new segment %s already known; skipping", segment.segment_id)
            continue
        known_ids.add(segment.segment_id)
        result.segments.append(segment)
        _logger.info(
            "new segment planned: %s (%s) with %d file(s), %.1fs",
            segment.segment_id[:12],
            segment.title,
            len(segment.file_ids),
            segment.duration_seconds,
        )

    return result
