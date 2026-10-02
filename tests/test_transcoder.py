from __future__ import annotations

import subprocess

import pytest

from tubetape import transcoder
from tubetape.scanner import ScannedFile, FILE_TYPE_IMAGE, FILE_TYPE_VIDEO
from tubetape.transcoder import (
    TranscodeConfig,
    build_concat_command,
    build_image_clip_command,
    build_video_clip_command,
    check_disk_space,
    segment_canvas,
    target_resolution,
    transcode_segment,
)

from conftest import make_heic, make_png, make_video


class TestTargetResolution:
    def test_no_upscale(self):
        assert target_resolution(100, 50, 3840, 2160) == (100, 50)

    def test_downscale(self):
        assert target_resolution(7680, 4320, 3840, 2160) == (3840, 2160)

    def test_downscale_aspect(self):
        w, h = target_resolution(4000, 3000, 1920, 1080)
        assert (w, h) == (1440, 1080)

    def test_even_dimensions(self):
        w, h = target_resolution(4001, 3001, 1920, 1080)
        assert w % 2 == 0 and h % 2 == 0


class TestCommandBuilding:
    def test_image_command_static(self):
        cfg = TranscodeConfig()
        cmd = build_image_clip_command("a.jpg", "out.mp4", 640, 480, 3.0, cfg)
        joined = " ".join(cmd)
        assert "-loop" in cmd and "1" in cmd
        assert "anullsrc=r=48000:cl=stereo" in joined
        assert "scale=640:480" in joined
        assert "pad=640:480" in joined
        assert "libx264" in cmd and "yuv420p" in cmd
        assert "aac" in cmd and "-crf" in cmd and "16" in cmd

    def test_image_command_ken_burns(self):
        cfg = TranscodeConfig(ken_burns=True)
        cmd = build_image_clip_command("a.jpg", "out.mp4", 640, 480, 3.0, cfg)
        assert any("zoompan" in part for part in cmd)

    def test_video_command(self):
        cfg = TranscodeConfig()
        cmd = build_video_clip_command("a.mp4", "out.mp4", 640, 480, cfg)
        joined = " ".join(cmd)
        assert "scale=640:480" in joined
        assert "libx264" in cmd and "aac" in cmd and "-ar" in cmd and "48000" in cmd

    def test_concat_command(self):
        cmd = build_concat_command("list.txt", "out.mp4")
        assert cmd[0] == "ffmpeg"
        assert "-c" in cmd and "copy" in cmd


class TestSegmentCanvas:
    def test_uses_first_file_resolution(self, tmp_path):
        make_png(tmp_path / "a.png", size=(100, 50))
        f = ScannedFile(
            file_id="x", abs_path=str(tmp_path / "a.png"), rel_path="a.png",
            name="a.png", type=FILE_TYPE_IMAGE, size_bytes=1,
            captured_epoch=0.0, duration_seconds=3.0, resolution="100x50",
        )
        assert segment_canvas([f], TranscodeConfig()) == (100, 50)

    def test_fallback_when_no_resolution(self):
        f = ScannedFile(
            file_id="x", abs_path="a.png", rel_path="a.png",
            name="a.png", type=FILE_TYPE_IMAGE, size_bytes=1,
            captured_epoch=0.0, duration_seconds=3.0, resolution=None,
        )
        assert segment_canvas([f], TranscodeConfig()) == (7680, 4320)

    def _file(self, w, h):
        return ScannedFile(
            file_id=f"{w}x{h}", abs_path="a", rel_path="a",
            name="a", type=FILE_TYPE_IMAGE, size_bytes=1,
            captured_epoch=0.0, duration_seconds=1.0, resolution=f"{w}x{h}",
        )

    def test_max_mode_uses_bounding_box(self):
        files = [self._file(1280, 720), self._file(3840, 2160)]
        assert segment_canvas(files, TranscodeConfig(canvas_mode="max")) == (3840, 2160)

    def test_first_mode_uses_first_file(self):
        files = [self._file(1280, 720), self._file(3840, 2160)]
        assert segment_canvas(files, TranscodeConfig(canvas_mode="first")) == (1280, 720)


class TestDiskSpace:
    def test_check_disk_space_ok(self, tmp_path):
        check_disk_space(str(tmp_path), 1)

    def test_check_disk_space_insufficient(self, tmp_path):
        with pytest.raises(OSError):
            check_disk_space(str(tmp_path), 10**18)


class TestTranscodeSegment:
    def test_image_segment(self, tmp_path):
        make_png(tmp_path / "img.png", size=(64, 48))
        f = ScannedFile(
            file_id="x", abs_path=str(tmp_path / "img.png"), rel_path="img.png",
            name="img.png", type=FILE_TYPE_IMAGE, size_bytes=100,
            captured_epoch=0.0, duration_seconds=2.0, resolution="64x48",
        )
        out = tmp_path / "seg.mp4"
        result, chapters = transcode_segment([f], str(out), TranscodeConfig())
        assert result == str(out)
        assert out.exists()
        assert chapters == [["0:00", "19700101-000000"]]
        _assert_h264_aac(str(out))

    def test_heic_image_segment(self, tmp_path):
        src = tmp_path / "img.heic"
        make_heic(src, size=(64, 48))
        f = ScannedFile(
            file_id="x", abs_path=str(src), rel_path="img.heic",
            name="img.heic", type=FILE_TYPE_IMAGE, size_bytes=100,
            captured_epoch=0.0, duration_seconds=2.0, resolution="64x48",
        )
        out = tmp_path / "seg.mp4"
        result, chapters = transcode_segment([f], str(out), TranscodeConfig())
        assert result == str(out)
        assert out.exists()
        assert chapters == [["0:00", "19700101-000000"]]
        _assert_h264_aac(str(out))

    def test_video_segment(self, tmp_path):
        src = tmp_path / "v.mp4"
        make_video(src, duration=1, size="64x48")
        f = ScannedFile(
            file_id="x", abs_path=str(src), rel_path="v.mp4",
            name="v.mp4", type=FILE_TYPE_VIDEO, size_bytes=100,
            captured_epoch=0.0, duration_seconds=1.0, resolution="64x48",
        )
        out = tmp_path / "seg.mp4"
        transcode_segment([f], str(out), TranscodeConfig())
        assert out.exists()
        _assert_h264_aac(str(out))

    def test_mixed_segment_concats(self, tmp_path):
        make_png(tmp_path / "img.png", size=(64, 48))
        make_video(tmp_path / "v.mp4", duration=1, size="64x48")
        img = ScannedFile(
            file_id="i", abs_path=str(tmp_path / "img.png"), rel_path="img.png",
            name="img.png", type=FILE_TYPE_IMAGE, size_bytes=100,
            captured_epoch=0.0, duration_seconds=1.0, resolution="64x48",
        )
        vid = ScannedFile(
            file_id="v", abs_path=str(tmp_path / "v.mp4"), rel_path="v.mp4",
            name="v.mp4", type=FILE_TYPE_VIDEO, size_bytes=100,
            captured_epoch=10.0, duration_seconds=1.0, resolution="64x48",
        )
        out = tmp_path / "seg.mp4"
        transcode_segment([img, vid], str(out), TranscodeConfig())
        assert out.exists()
        _assert_h264_aac(str(out))


def _assert_h264_aac(path):
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,pix_fmt", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    assert "h264" in proc.stdout, proc.stderr
    assert "yuv420p" in proc.stdout, proc.stderr

    aproc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,sample_rate,channels", "-of", "csv=p=0", path],
        capture_output=True, text=True,
    )
    assert "aac" in aproc.stdout, aproc.stderr
    assert "48000" in aproc.stdout, aproc.stderr
