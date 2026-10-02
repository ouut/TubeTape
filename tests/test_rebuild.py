from __future__ import annotations

import pytest

from tubetape.db import SEGMENT_STATUS_FAILED, SEGMENT_STATUS_SEALED, Database
from tubetape.planner import Segment
from tubetape.rebuild import RebuildConfig, Rebuilder, should_rebuild


def make_old_segment(db, segment_id="old-id", video_id="old-vid"):
    db.upsert_segment(
        segment_id,
        {
            "file_ids": ["a"],
            "range": ["2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z"],
            "duration_seconds": 60.0,
            "output_path": None,
            "youtube_video_id": video_id,
            "previous_video_ids": [],
            "status": SEGMENT_STATUS_SEALED,
            "chapters": [],
            "last_rebuilt_at": None,
            "attempts": 0,
            "error": None,
        },
    )
    return db.get_segment(segment_id)


def make_new_segment(segment_id="new-id", file_ids=None):
    return Segment(
        file_ids=file_ids or ["a", "b"],
        start_ts="2024-01-01T00:00:00Z",
        end_ts="2024-01-01T01:00:00Z",
        duration_seconds=90.0,
        segment_id=segment_id,
        title="t",
        replaces_segment_id="old-id",
    )


class CallRecorder:
    def __init__(self):
        self.calls = []

    def fn(self, name, result=None, exc=None):
        def _fn(*args, **kwargs):
            self.calls.append(name)
            if exc:
                raise exc
            return result
        return _fn


class TestShouldRebuild:
    def test_inside_cooldown(self):
        assert should_rebuild(100, None, 200, 86400, 600) is False

    def test_after_cooldown(self):
        assert should_rebuild(100, None, 100 + 86400, 86400, 600) is True

    def test_inside_quiet_period(self):
        assert should_rebuild(None, 100, 200, 86400, 600) is False

    def test_no_history(self):
        assert should_rebuild(None, None, 0, 86400, 600) is True


class TestRebuilder:
    def _rebuilder(self, db, recorder):
        return Rebuilder(
            db,
            transcode_fn=recorder.fn("transcode", ("/tmp/new.mp4", [["0:00", "x"]])),
            upload_fn=recorder.fn("upload", "new-vid"),
            verify_fn=recorder.fn("verify"),
            delete_fn=recorder.fn("delete"),
        )

    def test_success_order_and_commit(self):
        db = Database()
        make_old_segment(db)
        rec = CallRecorder()
        rb = self._rebuilder(db, rec)

        new_id = rb.rebuild("old-id", make_new_segment(), ["a", "b"], title="t", description="d")

        assert new_id == "new-vid"
        assert rec.calls == ["transcode", "upload", "verify", "delete"]
        # old record removed, new record committed
        assert db.get_segment("old-id") is None
        new = db.get_segment("new-id")
        assert new["youtube_video_id"] == "new-vid"
        assert new["previous_video_ids"] == ["old-vid"]
        assert new["status"] == SEGMENT_STATUS_SEALED
        assert new["chapters"] == [["0:00", "x"]]

    def test_upload_failure_keeps_old(self):
        db = Database()
        make_old_segment(db)
        rec = CallRecorder()
        rb = Rebuilder(
            db,
            transcode_fn=rec.fn("transcode", ("/tmp/new.mp4", [])),
            upload_fn=rec.fn("upload", exc=RuntimeError("upload failed")),
            verify_fn=rec.fn("verify"),
            delete_fn=rec.fn("delete"),
        )
        with pytest.raises(RuntimeError):
            rb.rebuild("old-id", make_new_segment(), ["a", "b"])
        assert rec.calls == ["transcode", "upload"]
        old = db.get_segment("old-id")
        assert old["youtube_video_id"] == "old-vid"  # preserved
        assert old["status"] == SEGMENT_STATUS_FAILED
        assert old["attempts"] == 1
        assert old["error"]

    def test_verify_failure_skips_delete(self):
        db = Database()
        make_old_segment(db)
        rec = CallRecorder()
        rb = Rebuilder(
            db,
            transcode_fn=rec.fn("transcode", ("/tmp/new.mp4", [])),
            upload_fn=rec.fn("upload", "new-vid"),
            verify_fn=rec.fn("verify", exc=RuntimeError("bad chapters")),
            delete_fn=rec.fn("delete"),
        )
        with pytest.raises(RuntimeError):
            rb.rebuild("old-id", make_new_segment(), ["a", "b"])
        assert rec.calls == ["transcode", "upload", "verify"]
        assert db.get_segment("old-id")["youtube_video_id"] == "old-vid"

    def test_delete_failure_preserves_old_video_id(self):
        db = Database()
        make_old_segment(db)
        rec = CallRecorder()
        rb = Rebuilder(
            db,
            transcode_fn=rec.fn("transcode", ("/tmp/new.mp4", [])),
            upload_fn=rec.fn("upload", "new-vid"),
            verify_fn=rec.fn("verify"),
            delete_fn=rec.fn("delete", exc=RuntimeError("delete failed")),
        )
        with pytest.raises(RuntimeError):
            rb.rebuild("old-id", make_new_segment(), ["a", "b"])
        assert rec.calls == ["transcode", "upload", "verify", "delete"]
        old = db.get_segment("old-id")
        assert old["youtube_video_id"] == "old-vid"  # kept
        assert old["status"] == SEGMENT_STATUS_FAILED

    def test_no_old_video_skips_delete(self):
        db = Database()
        make_old_segment(db, video_id=None)
        rec = CallRecorder()
        rb = self._rebuilder(db, rec)
        rb.rebuild("old-id", make_new_segment(), ["a", "b"])
        assert "delete" not in rec.calls
        assert db.get_segment("new-id")["previous_video_ids"] == []
