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


def test_build_segment_description():
    from tubetape.chapters import build_segment_description
    from tubetape.scanner import ScannedFile

    f1 = ScannedFile(file_id="p1", abs_path="/p1", rel_path="p1", name="p1.jpg", type="image", size_bytes=10, duration_seconds=3.0)
    f2 = ScannedFile(file_id="p2", abs_path="/p2", rel_path="p2", name="p2.jpg", type="image", size_bytes=10, duration_seconds=3.0)
    f3 = ScannedFile(file_id="v1", abs_path="/v1", rel_path="v1", name="v1.mp4", type="video", size_bytes=10, duration_seconds=85.0)
    f4 = ScannedFile(file_id="v2", abs_path="/v2", rel_path="v2", name="v2.mp4", type="video", size_bytes=10, duration_seconds=42.0)

    desc = build_segment_description([f1, f2, f3, f4])
    assert "照片: 2 张" in desc
    assert "视频: 2 条" in desc
    assert "总时长: 2:13" in desc
    assert "视频明细 (按构建顺序):" in desc
    assert "1. 1:25" in desc
    assert "2. 0:42" in desc
    # Filenames should NOT be in the pure duration list
    assert "v1.mp4" not in desc
    assert "v2.mp4" not in desc

