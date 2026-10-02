from __future__ import annotations

import time

from tubetape.watcher import (
    MtimeScanner,
    _MediaFileHandler,
    classify_capture_time,
    is_media_path,
    should_flush,
)


class TestIsMediaPath:
    def test_media(self):
        assert is_media_path("/x/photo.jpg") is True
        assert is_media_path("/x/video.MP4") is True

    def test_non_media(self):
        assert is_media_path("/x/notes.txt") is False


class TestClassifyCaptureTime:
    RANGES = [(100.0, 200.0), (300.0, 400.0)]

    def test_inside(self):
        assert classify_capture_time(150.0, self.RANGES) == "inside"

    def test_boundary_inclusive(self):
        assert classify_capture_time(100.0, self.RANGES) == "inside"
        assert classify_capture_time(200.0, self.RANGES) == "inside"

    def test_outside(self):
        assert classify_capture_time(250.0, self.RANGES) == "outside"

    def test_none_epoch(self):
        assert classify_capture_time(None, self.RANGES) == "outside"


class TestShouldFlush:
    def test_requested(self):
        assert should_flush(True, 0, 3600) is True

    def test_idle_timeout(self):
        assert should_flush(False, 3600, 3600) is True

    def test_not_yet(self):
        assert should_flush(False, 100, 3600) is False


class TestMediaFileHandler:
    def test_media_file_triggers_callback(self):
        seen = []
        handler = _MediaFileHandler(seen.append)

        class Event:
            is_directory = False
            src_path = "/x/photo.jpg"

        handler.on_created(Event())
        assert seen == ["/x/photo.jpg"]

    def test_non_media_ignored(self):
        seen = []
        handler = _MediaFileHandler(seen.append)

        class Event:
            is_directory = False
            src_path = "/x/notes.txt"

        handler.on_created(Event())
        assert seen == []

    def test_directory_ignored(self):
        seen = []
        handler = _MediaFileHandler(seen.append)

        class Event:
            is_directory = True
            src_path = "/x/dir.jpg"

        handler.on_created(Event())
        assert seen == []

    def test_move_into_dir_triggers_callback(self):
        seen = []
        handler = _MediaFileHandler(seen.append)

        class Event:
            is_directory = False
            src_path = "/x/tmp.download"
            dest_path = "/x/final.jpg"

        handler.on_moved(Event())
        assert seen == ["/x/final.jpg"]

    def test_move_of_non_media_ignored(self):
        seen = []
        handler = _MediaFileHandler(seen.append)

        class Event:
            is_directory = False
            src_path = "/x/a"
            dest_path = "/x/final.txt"

        handler.on_moved(Event())
        assert seen == []


class TestMtimeScanner:
    def test_steady_state_is_quiet(self, tmp_path):
        (tmp_path / "sub").mkdir()
        scanner = MtimeScanner(str(tmp_path))
        scanner.scan()  # baseline
        assert scanner.scan() == []

    def test_detects_new_file_in_subdir(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        scanner = MtimeScanner(str(tmp_path))
        scanner.scan()
        time.sleep(0.05)  # let the directory mtime advance past the cached value
        (sub / "a.jpg").write_bytes(b"x")
        changed = scanner.scan()
        assert str(sub) in changed

    def test_detects_new_subdirectory(self, tmp_path):
        scanner = MtimeScanner(str(tmp_path))
        scanner.scan()
        time.sleep(0.05)
        (tmp_path / "new").mkdir()
        changed = scanner.scan()
        assert str(tmp_path) in changed

    def test_ignores_hidden_dirs(self, tmp_path):
        hidden = tmp_path / ".hidden"
        hidden.mkdir()
        scanner = MtimeScanner(str(tmp_path))
        scanner.scan()
        time.sleep(0.05)
        (hidden / "a.jpg").write_bytes(b"x")
        assert scanner.scan() == []
