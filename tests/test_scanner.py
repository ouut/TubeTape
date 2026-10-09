from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from tubetape import scanner
from tubetape.db import Database

from conftest import (
    make_camera_photo,
    make_corrupt,
    make_heic,
    make_jpeg_with_exif,
    make_phone_video,
    make_png,
    make_video,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc

SAMPLE_STDERR = """\
ffmpeg version ...
  Duration: 00:00:10.50, start: 0.000000, bitrate: 100 kb/s
    Stream #0:0(und): Video: h264 (High), yuv420p, 1920x1080 [SAR 1:1 DAR 16:9], 25 fps
    creation_time   : 2024-01-01T15:30:00.000000Z
"""


class TestHashing:
    def test_stable_across_names(self, tmp_path):
        a = tmp_path / "a.jpg"
        b = tmp_path / "b.jpg"
        make_png(a)
        make_png(b)
        # Same content -> same id regardless of path/name.
        assert scanner.file_sha256(str(a)) == scanner.file_sha256(str(b))

    def test_different_content(self, tmp_path):
        a = tmp_path / "a.png"
        b = tmp_path / "b.png"
        make_png(a, size=(10, 10))
        make_png(b, size=(20, 20))
        assert scanner.file_sha256(str(a)) != scanner.file_sha256(str(b))

    def test_large_file_hash_is_stable(self, tmp_path):
        path = tmp_path / "big.bin"
        with open(path, "wb") as handle:
            handle.truncate(600 * 1024 * 1024)  # 600 MB sparse
        h1 = scanner.file_sha256(str(path))
        assert len(h1) == 64
        assert scanner.file_sha256(str(path)) == h1

    def test_sampled_hash_uses_head_middle_tail(self, tmp_path):
        chunk = scanner.HASH_CHUNK
        size = 4 * chunk
        a = tmp_path / "a.bin"
        b = tmp_path / "b.bin"
        with open(a, "wb") as f:
            f.write(b"A" * chunk + b"x" * (size - 2 * chunk) + b"A" * chunk)
        with open(b, "wb") as f:
            f.write(b"B" * chunk + b"x" * (size - 2 * chunk) + b"B" * chunk)
        # Different head bytes -> different hash (sample covers the head).
        assert scanner.file_sha256(str(a)) != scanner.file_sha256(str(b))
        # Same content -> stable hash.
        assert scanner.file_sha256(str(a)) == scanner.file_sha256(str(a))


class TestTimestampParsing:
    def test_parse_iso_utc_z(self):
        dt = scanner.parse_iso_utc("2024-01-01T15:30:00Z")
        assert dt == datetime(2024, 1, 1, 15, 30, 0, tzinfo=UTC)

    def test_parse_iso_utc_offset(self):
        dt = scanner.parse_iso_utc("2024-01-01T15:30:00+08:00")
        assert dt == datetime(2024, 1, 1, 7, 30, 0, tzinfo=UTC)

    def test_parse_iso_utc_naive_assumed_utc(self):
        dt = scanner.parse_iso_utc("2024-01-01T15:30:00")
        assert dt == datetime(2024, 1, 1, 15, 30, 0, tzinfo=UTC)

    def test_parse_iso_utc_microseconds(self):
        dt = scanner.parse_iso_utc("2024-01-01T15:30:00.500000Z")
        assert dt == datetime(2024, 1, 1, 15, 30, 0, 500000, tzinfo=UTC)

    def test_parse_iso_utc_invalid(self):
        assert scanner.parse_iso_utc("not a date") is None
        assert scanner.parse_iso_utc("") is None

    def test_parse_exif_datetime_timezone(self):
        dt = scanner.parse_exif_datetime("2024:01:01 15:30:00", SHANGHAI)
        assert dt == datetime(2024, 1, 1, 7, 30, 0, tzinfo=UTC)

    def test_parse_exif_datetime_invalid(self):
        assert scanner.parse_exif_datetime("garbage", SHANGHAI) is None


class TestParseFilenameTime:
    def test_wechat_format(self):
        dt = scanner.parse_filename_time("2020_12_16_01_00_IMG_6558.JPG", SHANGHAI)
        assert dt == datetime(2020, 12, 15, 17, 0, 0, tzinfo=UTC)

    def test_wechat_with_seconds(self):
        dt = scanner.parse_filename_time("2020_12_16_01_00_30_IMG_1.jpg", SHANGHAI)
        assert dt == datetime(2020, 12, 15, 17, 0, 30, tzinfo=UTC)

    def test_img_prefix(self):
        dt = scanner.parse_filename_time("IMG_20201216_010000.jpg", SHANGHAI)
        assert dt == datetime(2020, 12, 15, 17, 0, 0, tzinfo=UTC)

    def test_vid_prefix(self):
        dt = scanner.parse_filename_time("VID_20201216_010000.mp4", SHANGHAI)
        assert dt == datetime(2020, 12, 15, 17, 0, 0, tzinfo=UTC)

    def test_compact_minutes_only(self):
        dt = scanner.parse_filename_time("20201216_0100.jpg", SHANGHAI)
        assert dt == datetime(2020, 12, 15, 17, 0, 0, tzinfo=UTC)

    def test_hyphen_format(self):
        dt = scanner.parse_filename_time("2020-12-16 01-00-00.jpg", SHANGHAI)
        assert dt == datetime(2020, 12, 15, 17, 0, 0, tzinfo=UTC)

    def test_no_date(self):
        assert scanner.parse_filename_time("IMG_6558.JPG", SHANGHAI) is None
        assert scanner.parse_filename_time("photo.jpg", SHANGHAI) is None

    def test_invalid_date(self):
        # month 13, day 32, hour 25 are all out of range
        assert scanner.parse_filename_time("2020_13_01_00_00_x.jpg", SHANGHAI) is None
        assert scanner.parse_filename_time("2020_01_32_00_00_x.jpg", SHANGHAI) is None
        assert scanner.parse_filename_time("2020_01_01_25_00_x.jpg", SHANGHAI) is None


class TestFfmpegParsing:
    def test_duration(self):
        assert scanner._parse_ffmpeg_duration(SAMPLE_STDERR) == pytest.approx(10.5)

    def test_resolution(self):
        assert scanner._parse_ffmpeg_resolution(SAMPLE_STDERR) == "1920x1080"

    def test_creation_time(self):
        assert scanner._parse_ffmpeg_creation_time(SAMPLE_STDERR) == "2024-01-01T15:30:00.000000Z"


class TestProbeVideo:
    def test_probe(self, tmp_path):
        video = tmp_path / "v.mp4"
        make_video(video, duration=2, size="320x240")
        info = scanner.probe_video(str(video))
        assert info["duration_seconds"] == pytest.approx(2.0, abs=0.1)
        assert info["resolution"] == "320x240"
        assert info["creation_time"]


class TestSourceClassification:
    def test_camera_photo(self, tmp_path):
        make_camera_photo(tmp_path / "cam.jpg")
        files, _, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        assert files[0].source == "camera"

    def test_plain_image_is_other(self, tmp_path):
        make_png(tmp_path / "shot.png")
        files, _, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        assert files[0].source == "other"

    def test_heic_photo_is_camera(self, tmp_path):
        make_heic(tmp_path / "cam.heic")
        files, errors, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        assert errors == []
        assert files[0].source == "camera"
        assert files[0].captured_at_utc == "2024-01-01T07:30:00Z"

    def test_phone_video(self, tmp_path):
        make_phone_video(tmp_path / "phone.mov")
        files, _, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        assert files[0].source == "camera"

    def test_plain_video_is_other(self, tmp_path):
        make_video(tmp_path / "dl.mp4", creation_time=None)
        files, _, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        assert files[0].source == "other"

    def test_ffmpeg_make_parser(self):
        stderr = "    make            : Apple\n    encoder         : Lavf58.76.100\n"
        assert scanner._parse_ffmpeg_make(stderr) == "Apple"
        assert scanner._parse_ffmpeg_make("encoder: Lavf58\n") is None


class TestFilters:
    def test_only_camera_photos(self, tmp_path):
        make_camera_photo(tmp_path / "cam.jpg")
        make_png(tmp_path / "shot.png")
        files, _, skipped = scanner.scan_files(str(tmp_path), SHANGHAI, only_camera_photos=True)
        assert [f.name for f in files] == ["cam.jpg"]
        assert [s["path"] for s in skipped] == ["shot.png"]

    def test_only_phone_videos(self, tmp_path):
        make_phone_video(tmp_path / "phone.mov")
        make_video(tmp_path / "dl.mp4", creation_time=None)
        files, _, skipped = scanner.scan_files(str(tmp_path), SHANGHAI, only_phone_videos=True)
        assert [f.name for f in files] == ["phone.mov"]
        assert [s["path"] for s in skipped] == ["dl.mp4"]


class TestScanFiles:
    def test_image_exif_time(self, tmp_path):
        img = tmp_path / "p.jpg"
        make_jpeg_with_exif(img, "2024:01:01 15:30:00")
        files, errors, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        assert errors == []
        assert len(files) == 1
        f = files[0]
        assert f.type == "image"
        assert f.captured_at_utc == "2024-01-01T07:30:00Z"
        assert f.missing_meta is False
        assert f.resolution == "100x50"
        assert f.duration_seconds == 3.0

    def test_image_without_exif_uses_mtime(self, tmp_path):
        img = tmp_path / "p.png"
        make_png(img)
        files, errors, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        assert len(files) == 1
        f = files[0]
        assert f.missing_meta is True
        assert f.time_source == "mtime"
        assert f.captured_at_utc is not None
        # mtime should be close to now (UTC)
        dt = scanner.parse_iso_utc(f.captured_at_utc)
        now = datetime.now(UTC)
        assert abs((now - dt).total_seconds()) < 300

    def test_heic_exif_time_and_resolution(self, tmp_path):
        make_heic(tmp_path / "p.heic", dt_str="2024:01:01 15:30:00", size=(100, 80))
        files, errors, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        assert errors == []
        assert len(files) == 1
        f = files[0]
        assert f.type == "image"
        assert f.captured_at_utc == "2024-01-01T07:30:00Z"
        assert f.missing_meta is False
        assert f.resolution == "100x80"
        assert f.source == "camera"

    def test_image_filename_time_fallback(self, tmp_path):
        from PIL import Image

        img = tmp_path / "2020_12_16_01_00_IMG_6558.jpg"
        Image.new("RGB", (10, 10), "green").save(img)  # no EXIF DateTimeOriginal
        files, errors, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        assert len(files) == 1
        f = files[0]
        assert f.captured_at_utc == "2020-12-15T17:00:00Z"
        assert f.missing_meta is True
        assert f.time_source == "filename"

    def test_metadata_beats_filename(self, tmp_path):
        # EXIF DateTimeOriginal wins over a filename timestamp.
        img = tmp_path / "2020_12_16_01_00_x.jpg"
        make_jpeg_with_exif(img, "2021:01:01 00:00:00")
        files, _, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        assert files[0].captured_at_utc == "2020-12-31T16:00:00Z"
        assert files[0].time_source == "metadata"
        assert files[0].missing_meta is False

    def test_video_creation_time(self, tmp_path):
        video = tmp_path / "v.mp4"
        make_video(video, creation_time="2024-01-01T15:30:00Z", duration=2)
        files, errors, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        assert len(files) == 1
        f = files[0]
        assert f.type == "video"
        assert f.captured_at_utc == "2024-01-01T15:30:00Z"
        assert f.missing_meta is False
        assert f.duration_seconds == pytest.approx(2.0, abs=0.1)
        assert f.resolution == "320x240"

    def test_corrupt_file_skipped(self, tmp_path):
        make_corrupt(tmp_path / "bad.jpg")
        files, errors, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        assert files == []
        assert len(errors) == 1
        assert errors[0]["path"] == "bad.jpg"

    def test_non_media_ignored(self, tmp_path):
        (tmp_path / "notes.txt").write_text("hello")
        files, errors, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        assert files == []
        assert errors == []

    def test_hidden_files_and_dirs_skipped(self, tmp_path):
        make_png(tmp_path / "visible.png", size=(10, 10))
        # dotfile with a media extension must be ignored
        make_png(tmp_path / ".hidden.png", size=(10, 10))
        # hidden directory with media inside must be ignored
        (tmp_path / ".hidden_dir").mkdir()
        make_png(tmp_path / ".hidden_dir" / "x.png", size=(10, 10))
        files, errors, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        assert [f.name for f in files] == ["visible.png"]
        assert errors == []

    def test_file_id_stable_after_rename(self, tmp_path):
        make_png(tmp_path / "a.png")
        files_a, _, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        id_a = files_a[0].file_id
        os.rename(tmp_path / "a.png", tmp_path / "renamed.png")
        files_b, _, _ = scanner.scan_files(str(tmp_path), SHANGHAI)
        assert files_b[0].file_id == id_a

    def test_image_duration_configurable(self, tmp_path):
        make_png(tmp_path / "p.png")
        files, _, _ = scanner.scan_files(str(tmp_path), SHANGHAI, image_duration=5.0)
        assert files[0].duration_seconds == 5.0


class TestScanReconcile:
    def test_new_processed_deleted(self, tmp_path):
        # Distinct content so the two files get distinct content-hash IDs.
        make_png(tmp_path / "existing.png", size=(10, 10))
        make_png(tmp_path / "new.png", size=(20, 20))

        # Pre-populate DB with "existing" (matches disk) and "gone" (absent).
        existing_id = scanner.file_sha256(str(tmp_path / "existing.png"))
        db = Database()
        db.upsert_file(existing_id, {"path": "existing.png", "type": "image"})
        db.upsert_file("gone-id", {"path": "gone.png", "type": "image"})

        result = scanner.scan(str(tmp_path), db, SHANGHAI)
        assert sorted(result.new_file_ids) == [scanner.file_sha256(str(tmp_path / "new.png"))]
        assert result.processed_file_ids == [existing_id]
        assert result.deleted_file_ids == ["gone-id"]

    def test_duplicate_files_prevent_rescan(self, tmp_path):
        # Two files with identical content (duplicate files on disk)
        make_png(tmp_path / "orig.png", size=(10, 10))
        make_png(tmp_path / "copy.png", size=(10, 10))
        file_id = scanner.file_sha256(str(tmp_path / "orig.png"))

        db = Database()
        db.upsert_file(file_id, {"path": "orig.png", "type": "image", "alt_paths": ["copy.png"]})

        # Scan should identify identical path set and skip scanning
        res = scanner.scan(str(tmp_path), db, SHANGHAI)
        assert len(res.new_file_ids) == 0
        assert len(res.deleted_file_ids) == 0
        assert len(res.files) == 1
        assert res.files[0].file_id == file_id

    def test_skipped_files_prevent_rescan(self, tmp_path):
        # File skipped because it's not a camera photo
        make_png(tmp_path / "screenshot.png", size=(10, 10))
        db = Database()
        db.skipped_files["screenshot.png"] = "not a camera photo"

        res = scanner.scan(str(tmp_path), db, SHANGHAI, only_camera_photos=True)
        assert len(res.new_file_ids) == 0
        assert len(res.deleted_file_ids) == 0

    def test_normalized_paths_match(self, tmp_path):
        (tmp_path / "sub").mkdir()
        make_png(tmp_path / "sub" / "img.png", size=(10, 10))
        fid = scanner.file_sha256(str(tmp_path / "sub" / "img.png"))

        db = Database()
        # Path stored with leading ./ or backslashes
        db.upsert_file(fid, {"path": ".\\sub\\img.png", "type": "image"})

        res = scanner.scan(str(tmp_path), db, SHANGHAI)
        assert len(res.new_file_ids) == 0
        assert len(res.deleted_file_ids) == 0

    def test_nas_eadir_ignored(self, tmp_path):
        make_png(tmp_path / "normal.png", size=(10, 10))
        eadir = tmp_path / "@eaDir"
        eadir.mkdir()
        make_png(eadir / "thumb.png", size=(5, 5))

        candidates = scanner.collect_media_files(str(tmp_path))
        names = [c[2] for c in candidates]
        assert "normal.png" in names
        assert "thumb.png" not in names
