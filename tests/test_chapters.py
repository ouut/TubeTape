from __future__ import annotations

import pytest

from tubetape.chapters import (
    build_chapters,
    chapters_text,
    format_chapter_timestamp,
)


class TestFormatChapterTimestamp:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "0:00"),
            (3, "0:03"),
            (20, "0:20"),
            (65, "1:05"),
            (600, "10:00"),
            (3600, "1:00:00"),
            (3661, "1:01:01"),
        ],
    )
    def test_format(self, seconds, expected):
        assert format_chapter_timestamp(seconds) == expected


class TestBuildChapters:
    def test_single_file(self):
        assert build_chapters([(3.0, "20240101-153000")]) == [["0:00", "20240101-153000"]]

    def test_all_long_files(self):
        items = [(20.0, "A"), (20.0, "B"), (20.0, "C")]
        assert build_chapters(items) == [["0:00", "A"], ["0:20", "B"], ["0:40", "C"]]

    def test_short_files_merge(self):
        # Two 3-second files: the second would start at 0:03 (< 10s), so it merges.
        items = [(3.0, "A"), (3.0, "B")]
        assert build_chapters(items) == [["0:00", "A"]]

    def test_merge_keeps_first_title(self):
        items = [(3.0, "A"), (3.0, "B"), (10.0, "C")]
        chapters = build_chapters(items)
        assert chapters[0] == ["0:00", "A"]
        # C starts at 0:06, still < 10s after 0:00, so it also merges.
        assert len(chapters) == 1

    def test_boundary_exactly_10s(self):
        items = [(10.0, "A"), (10.0, "B")]
        assert build_chapters(items) == [["0:00", "A"], ["0:10", "B"]]

    def test_min_gap_custom(self):
        items = [(5.0, "A"), (5.0, "B"), (5.0, "C")]
        assert build_chapters(items, min_gap=5.0) == [
            ["0:00", "A"],
            ["0:05", "B"],
            ["0:10", "C"],
        ]


def test_chapters_text():
    chapters = [["0:00", "20240101-153000"], ["0:20", "20240101-153020"]]
    assert chapters_text(chapters) == "0:00 20240101-153000\n0:20 20240101-153020"
