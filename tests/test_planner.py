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
    def test_file_inside_range_creates_historical_segment_not_rebuild(self):
        # Under append-only rules, historical files do not modify existing segments;
        # they form an independent historical patch segment.
        sid, record = _existing_record(["a"], 100, 150)
        files = [sf("a", 100, 10), sf("new", 120, 10)]
        result = plan(files, {sid: record}, segment_duration=20)
        assert len(result.segments) == 1
        assert result.segments[0].is_rebuild is False
        assert result.segments[0].file_ids == ["new"]
        assert sid in result.skipped_segment_ids

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

    def test_dedup_identical_new_segment(self):
        files = [sf("a", 100, 10), sf("b", 101, 10)]
        expected_id = segment_id(["a", "b"], ())
        first = plan(files, {}, segment_duration=20, flush=True)
        assert first.segments[0].segment_id == expected_id
        second = plan(files, {expected_id: {"file_ids": ["a", "b"], "range": [iso(100), iso(101)]}}, segment_duration=20, flush=True)
        assert second.segments == []
        assert expected_id in second.skipped_segment_ids

    def test_undated_files_sort_first_and_compact_title(self):
        # Undated files have captured_epoch=None and captured_at_utc=None
        undated1 = ScannedFile(
            file_id="u1", abs_path="/x/u1", rel_path="u1", name="u1", type="image",
            size_bytes=100, captured_at_utc=None, captured_epoch=None, duration_seconds=3.0,
        )
        undated2 = ScannedFile(
            file_id="u2", abs_path="/x/u2", rel_path="u2", name="u2", type="image",
            size_bytes=100, captured_at_utc=None, captured_epoch=None, duration_seconds=3.0,
        )
        dated = sf("d1", 1000, 10.0)

        # First run: undated sort first
        result = plan([dated, undated2, undated1], {}, segment_duration=10.0, flush=True)
        # undated1 + undated2 = 6s (< 10s), dated = 10s -> 2 segments
        assert len(result.segments) == 2
        # First segment is undated
        seg0 = result.segments[0]
        assert seg0.file_ids == ["u1", "u2"]
        assert seg0.title.startswith("19700101_000000-19700101_000000_[")
        assert seg0.start_ts == "1970-01-01T00:00:00Z"

        # Second segment is dated
        seg1 = result.segments[1]
        assert seg1.file_ids == ["d1"]

    def test_multiple_historical_files_respect_capacity(self):
        # Existing segment spans 100..200
        sid, record = _existing_record(["a"], 100, 200)
        # Add 4 historical files totaling 40s with segment_duration=20
        h_files = [
            sf("h1", 110, 10),
            sf("h2", 120, 10),
            sf("h3", 130, 10),
            sf("h4", 140, 10),
        ]
        files = [sf("a", 100, 10)] + h_files
        result = plan(files, {sid: record}, segment_duration=20)

        # Existing segment is untouched
        assert sid in result.skipped_segment_ids
        # History files are split into two 20s segments
        assert len(result.segments) == 2
        assert result.segments[0].duration_seconds == 20.0
        assert result.segments[1].duration_seconds == 20.0
        assert result.segments[0].file_ids == ["h1", "h2"]
        assert result.segments[1].file_ids == ["h3", "h4"]


