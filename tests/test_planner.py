from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tubetape import planner
from tubetape.planner import Plan, Segment, greedy_pack, plan, segment_id
from tubetape.scanner import ScannedFile, format_iso_utc

UTC = timezone.utc


def sf(file_id, epoch, duration, ftype="image"):
    return ScannedFile(
        file_id=file_id,
        abs_path=f"/x/{file_id}",
        rel_path=file_id,
        name=file_id,
        type=ftype,
        size_bytes=100,
        captured_at_utc=format_iso_utc(datetime.fromtimestamp(epoch, tz=UTC)),
        captured_epoch=epoch,
        duration_seconds=duration,
    )


def iso(epoch):
    return format_iso_utc(datetime.fromtimestamp(epoch, tz=UTC))


class TestSegmentId:
    def test_deterministic(self):
        assert segment_id(["a", "b", "c"], ()) == segment_id(["c", "a", "b"], ())

    def test_content_change(self):
        assert segment_id(["a", "b"], ()) != segment_id(["a", "b", "c"], ())

    def test_params_change(self):
        assert segment_id(["a"], ("p1",)) != segment_id(["a"], ("p2",))

    def test_params_order_matters(self):
        assert segment_id(["a"], (1, 2)) != segment_id(["a"], (2, 1))


class TestGreedyPack:
    def test_basic_pack(self):
        files = [
            sf("a", 100, 10),
            sf("b", 101, 10),
            sf("c", 102, 10),
            sf("d", 103, 10),
        ]
        groups = greedy_pack(files, 25)
        assert [[f.file_id for f in g] for g in groups] == [["a", "b"], ["c", "d"]]

    def test_exact_boundary(self):
        files = [sf("a", 100, 10), sf("b", 101, 10), sf("c", 102, 10)]
        groups = greedy_pack(files, 20)
        assert [[f.file_id for f in g] for g in groups] == [["a", "b"], ["c"]]

    def test_oversized_single_file_standalone(self):
        files = [sf("a", 100, 5), sf("big", 101, 100), sf("b", 102, 5)]
        groups = greedy_pack(files, 20)
        assert [[f.file_id for f in g] for g in groups] == [["a"], ["big"], ["b"]]

    def test_sort_by_capture_time(self):
        files = [sf("later", 200, 5), sf("earlier", 100, 5)]
        groups = greedy_pack(files, 20)
        assert [f.file_id for f in groups[0]] == ["earlier", "later"]


class TestMakeSegment:
    def test_title_and_range(self):
        seg = planner._make_segment([sf("a", 100, 5), sf("b", 150, 5)], ())
        assert seg.title.startswith("19700101-000140 - 19700101-000230")
        assert seg.title.endswith(f"[{seg.segment_id[:16]}]")
        assert seg.start_ts == iso(100)
        assert seg.end_ts == iso(150)
        assert seg.duration_seconds == 10.0

    def test_file_ids_sorted_by_time(self):
        seg = planner._make_segment([sf("b", 150, 5), sf("a", 100, 5)], ())
        assert seg.file_ids == ["a", "b"]

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            planner._make_segment([], ())


class TestPlanFirstRun:
    def test_no_existing(self):
        files = [sf("a", 100, 10), sf("b", 101, 10), sf("c", 102, 10)]
        result = plan(files, {}, segment_duration=20, flush=True)
        assert len(result.segments) == 2
        assert result.segments[0].file_ids == ["a", "b"]
        assert result.segments[1].file_ids == ["c"]
        assert result.skipped_segment_ids == []
        assert result.pending_files == []

    def test_trailing_partial_held_without_flush(self):
        files = [sf("a", 100, 10), sf("b", 101, 5)]
        result = plan(files, {}, segment_duration=20)
        assert result.segments == []
        assert [f.file_id for f in result.pending_files] == ["a", "b"]

    def test_trailing_partial_sealed_with_flush(self):
        files = [sf("a", 100, 10), sf("b", 101, 5)]
        result = plan(files, {}, segment_duration=20, flush=True)
        assert [s.file_ids for s in result.segments] == [["a", "b"]]
        assert result.pending_files == []

    def test_oversized_is_not_partial(self):
        files = [sf("big", 100, 100)]
        result = plan(files, {}, segment_duration=20)
        assert [s.file_ids for s in result.segments] == [["big"]]
        assert result.pending_files == []


def _existing_record(file_ids, start_epoch, end_epoch, sid=None):
    record = {
        "file_ids": file_ids,
        "range": [iso(start_epoch), iso(end_epoch)],
    }
    if sid is None:
        sid = segment_id(file_ids, ())
    return sid, record


class TestPlanLaterRuns:
    def test_file_inside_range_triggers_rebuild(self):
        sid, record = _existing_record(["a"], 100, 150)
        files = [sf("a", 100, 10), sf("new", 120, 10)]
        result = plan(files, {sid: record}, segment_duration=20)
        assert len(result.segments) == 1
        assert result.segments[0].is_rebuild is True
        assert result.segments[0].replaces_segment_id == sid
        assert result.segments[0].file_ids == ["a", "new"]

    def test_unchanged_segment_skipped(self):
        sid, record = _existing_record(["a"], 100, 150)
        files = [sf("a", 100, 10)]
        result = plan(files, {sid: record}, segment_duration=20)
        assert result.segments == []
        assert result.skipped_segment_ids == [sid]

    def test_file_outside_range_new_segment(self):
        sid, record = _existing_record(["a"], 100, 150)
        files = [sf("a", 100, 10), sf("outside", 999, 10)]
        result = plan(files, {sid: record}, segment_duration=20, flush=True)
        assert result.skipped_segment_ids == [sid]
        assert len(result.segments) == 1
        assert result.segments[0].file_ids == ["outside"]
        assert result.segments[0].is_rebuild is False

    def test_deleted_file_drops_from_segment(self):
        # "gone" was in the segment but no longer exists on disk.
        sid, record = _existing_record(["a", "gone"], 100, 150)
        files = [sf("a", 100, 10)]
        result = plan(files, {sid: record}, segment_duration=20)
        assert len(result.segments) == 1
        assert result.segments[0].file_ids == ["a"]
        assert result.segments[0].is_rebuild is True

    def test_params_change_triggers_rebuild(self):
        sid = segment_id(["a"], ("old",))
        record = {"file_ids": ["a"], "range": [iso(100), iso(150)]}
        files = [sf("a", 100, 10)]
        result = plan(files, {sid: record}, segment_duration=20, params=("new",))
        assert len(result.segments) == 1
        assert result.segments[0].is_rebuild is True

    def test_dedup_identical_new_segment(self):
        files = [sf("a", 100, 10), sf("b", 101, 10)]
        # Pre-compute what the packed segment ID would be.
        expected_id = segment_id(["a", "b"], ())
        # First plan produces it; second plan (same files) must skip it.
        first = plan(files, {}, segment_duration=20, flush=True)
        assert first.segments[0].segment_id == expected_id
        second = plan(files, {expected_id: {"file_ids": ["a", "b"], "range": [iso(100), iso(101)]}}, segment_duration=20, flush=True)
        # The files fall inside the existing range, and the recomputed ID matches.
        assert second.segments == []
        assert expected_id in second.skipped_segment_ids

    def test_rebuild_capacity_clamping_and_overflow(self):
        # Existing segment spans 100..500 with 1 file of 15s. Max duration is 20s.
        sid, record = _existing_record(["a"], 100, 500)
        # New files inside the range:
        # b (5s) fits into existing segment (15 + 5 = 20s)
        # c (10s) does not fit (would exceed 20s), so overflows into free_files
        # d (10s) overflows into free_files
        files = [sf("a", 100, 15), sf("b", 120, 5), sf("c", 130, 10), sf("d", 140, 10)]
        result = plan(files, {sid: record}, segment_duration=20, flush=True)

        # The rebuilt segment has a and b (total 20s)
        rebuilt = [s for s in result.segments if s.replaces_segment_id == sid]
        assert len(rebuilt) == 1
        assert rebuilt[0].file_ids == ["a", "b"]
        assert rebuilt[0].duration_seconds == 20.0

        # c and d are packed into a new segment (10 + 10 = 20s)
        new_segs = [s for s in result.segments if s.replaces_segment_id != sid]
        assert len(new_segs) == 1
        assert new_segs[0].file_ids == ["c", "d"]
        assert new_segs[0].duration_seconds == 20.0

    def test_rebuild_candidate_prefers_narrower_range(self):
        # Segment 1 is wide: 100..1000
        # Segment 2 is narrow: 200..300
        sid_wide, rec_wide = _existing_record(["w"], 100, 1000)
        sid_narrow, rec_narrow = _existing_record(["n"], 200, 300)

        # New file at 250 (falls in both). Should prefer narrow segment.
        files = [sf("w", 100, 5), sf("n", 200, 5), sf("item", 250, 5)]
        result = plan(files, {sid_wide: rec_wide, sid_narrow: rec_narrow}, segment_duration=20)

        # Narrow segment gets "item"
        narrow_rebuild = [s for s in result.segments if s.replaces_segment_id == sid_narrow]
        assert len(narrow_rebuild) == 1
        assert "item" in narrow_rebuild[0].file_ids
        assert sid_wide in result.skipped_segment_ids

    def test_rebuild_splits_oversized_legacy_combined(self):
        # Existing segment record had 5 files totaling 50s due to prior unconstrained bug
        # When replanned with segment_duration=20, it must split into segments <= 20s
        legacy_files = ["f1", "f2", "f3", "f4", "f5"]
        sid, record = _existing_record(legacy_files, 100, 500)
        files = [sf(f"f{i}", 100 + i * 10, 10) for i in range(1, 6)]
        result = plan(files, {sid: record}, segment_duration=20, flush=True)

        # Total duration = 50s. Split into groups of 20s (2 files), 20s (2 files), 10s (1 file)
        assert len(result.segments) == 3
        for s in result.segments:
            assert s.duration_seconds <= 20.0
        # Exactly one replaces sid
        assert sum(1 for s in result.segments if s.replaces_segment_id == sid) == 1

