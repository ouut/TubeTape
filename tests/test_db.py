from __future__ import annotations

import os

import pytest

from tubetape.db import SCHEMA_VERSION, Database, DatabaseError


def make_file_record(file_id="abc123", **overrides):
    record = {
        "path": "sub/dir/photo.jpg",
        "name": "photo.jpg",
        "type": "image",
        "captured_at_utc": "2024-01-01T15:30:00Z",
        "resolution": "4000x3000",
        "duration_seconds": 3.0,
        "size_bytes": 1234567,
        "sha256": file_id,
        "location": None,
        "missing_meta": False,
    }
    record.update(overrides)
    return record


def make_segment_record(segment_id="seg1", **overrides):
    record = {
        "file_ids": ["abc123"],
        "range": ["2024-01-01T15:30:00Z", "2024-01-01T15:40:00Z"],
        "duration_seconds": 600.0,
        "output_path": "/tmp/seg1.mp4",
        "youtube_video_id": None,
        "previous_video_ids": [],
        "status": "pending",
        "chapters": [["0:00", "20240101-153000"]],
        "last_rebuilt_at": None,
        "attempts": 0,
        "error": None,
    }
    record.update(overrides)
    return record


class TestNewDatabase:
    def test_defaults(self):
        db = Database()
        assert db.version == SCHEMA_VERSION
        assert db.files == {}
        assert db.segments == {}
        assert db.path is None

    def test_save_without_path_raises(self):
        db = Database()
        with pytest.raises(DatabaseError):
            db.save()


class TestRoundTrip:
    def test_round_trip_preserves_data(self, tmp_path):
        path = tmp_path / "tubetape.json"
        db = Database(path=str(path))
        db.upsert_file("abc", make_file_record("abc", location={"lat": 1.0, "lng": 2.0}))
        db.upsert_segment("seg1", make_segment_record("seg1", youtube_video_id="vid123"))
        db.save()

        loaded = Database.load(str(path))
        assert loaded.to_dict() == db.to_dict()
        assert loaded.files["abc"]["location"] == {"lat": 1.0, "lng": 2.0}
        assert loaded.get_segment("seg1")["youtube_video_id"] == "vid123"

    def test_load_missing_returns_empty(self, tmp_path):
        path = tmp_path / "none.json"
        db = Database.load(str(path))
        assert db.files == {}
        assert db.path == str(path)

    def test_save_creates_parent_directories(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "db.json"
        Database(path=str(path)).save()
        assert os.path.exists(path)

    def test_round_trip_preserves_config(self, tmp_path):
        path = tmp_path / "tubetape.json"
        db = Database(path=str(path), config={"crf": 18, "segment_duration": 1800.0})
        db.save()

        loaded = Database.load(str(path))
        assert loaded.config == {"crf": 18, "segment_duration": 1800.0}
        assert loaded.to_dict() == db.to_dict()

    def test_migrates_legacy_config_json(self, tmp_path):
        # When config.json exists but tubetape.json has no config key
        legacy_file = tmp_path / "config.json"
        legacy_file.write_text('{"crf": 20, "keep_segments": 3}', encoding="utf-8")

        # Case A: tubetape.json does not exist yet
        db_path = tmp_path / "tubetape.json"
        loaded = Database.load(str(db_path))
        assert loaded.config["crf"] == 20
        assert loaded.config["keep_segments"] == 3

        # Case B: tubetape.json exists without config
        db_path.write_text('{"version": 1, "files": {}, "segments": {}}', encoding="utf-8")
        loaded_b = Database.load(str(db_path))
        assert loaded_b.config["crf"] == 20
        assert loaded_b.config["keep_segments"] == 3


class TestAtomicWrite:
    def test_no_temp_files_left_behind(self, tmp_path):
        path = tmp_path / "db.json"
        Database(path=str(path)).save()
        leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
        assert leftovers == []

    def test_failed_replace_preserves_original(self, tmp_path, monkeypatch):
        path = tmp_path / "db.json"
        db = Database(path=str(path))
        db.upsert_file("abc", make_file_record("abc"))
        db.save()

        original = path.read_text()

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            db.save()

        assert path.read_text() == original
        leftovers = [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]
        assert leftovers == []

    def test_corrupt_json_raises(self, tmp_path):
        path = tmp_path / "db.json"
        path.write_text("{ not valid json", encoding="utf-8")
        with pytest.raises(DatabaseError):
            Database.load(str(path))

    def test_non_object_json_raises(self, tmp_path):
        path = tmp_path / "db.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(DatabaseError):
            Database.load(str(path))


class TestFileHelpers:
    def test_upsert_remove(self):
        db = Database()
        db.upsert_file("abc", make_file_record("abc"))
        assert db.files["abc"]["name"] == "photo.jpg"
        db.remove_file("abc")
        assert db.files.get("abc") is None

    def test_remove_missing_is_noop(self):
        db = Database()
        db.remove_file("nope")  # should not raise


class TestSegmentHelpers:
    def test_upsert_get(self):
        db = Database()
        db.upsert_segment("seg1", make_segment_record("seg1"))
        assert db.get_segment("seg1")["status"] == "pending"
