from __future__ import annotations

from tubetape.reconcile import (
    SEGMENT_ID_MARKER,
    embed_segment_id,
    extract_segment_id,
    fetch_remote_index,
    get_uploads_playlist_id,
)


class TestEmbedExtract:
    def test_embed_then_extract(self):
        desc = embed_segment_id("0:00 a\n1:00 b", "abc123")
        assert SEGMENT_ID_MARKER in desc
        assert extract_segment_id(desc) == "abc123"

    def test_embed_is_idempotent(self):
        once = embed_segment_id("x", "abc")
        assert embed_segment_id(once, "abc") == once

    def test_embed_empty_description(self):
        desc = embed_segment_id("", "abc")
        assert extract_segment_id(desc) == "abc"

    def test_extract_missing(self):
        assert extract_segment_id("no marker here") is None
        assert extract_segment_id("") is None
        assert extract_segment_id(None) is None


class _Exec:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _Channels:
    def __init__(self, uploads):
        self._uploads = uploads

    def list(self, **kwargs):
        items = (
            [{"contentDetails": {"relatedPlaylists": {"uploads": self._uploads}}}]
            if self._uploads
            else []
        )
        return _Exec({"items": items})


class _PlaylistItems:
    def __init__(self, pages):
        self._pages = list(pages)

    def list(self, **kwargs):
        return _Exec(self._pages.pop(0))


class _FakeService:
    def __init__(self, uploads, pages):
        self._channels = _Channels(uploads)
        self._items = _PlaylistItems(pages)

    def channels(self):
        return self._channels

    def playlistItems(self):
        return self._items


def _item(video_id, description):
    return {
        "snippet": {
            "resourceId": {"videoId": video_id},
            "description": description,
        }
    }


class TestGetUploadsPlaylistId:
    def test_returns_uploads_playlist(self):
        assert get_uploads_playlist_id(_FakeService("UU123", [])) == "UU123"

    def test_none_when_no_items(self):
        assert get_uploads_playlist_id(_FakeService(None, [])) is None


class TestFetchRemoteIndex:
    def test_collects_marked_videos_across_pages(self):
        pages = [
            {
                "items": [
                    _item("v1", embed_segment_id("d", "s1")),
                    _item("v2", "no marker"),
                ],
                "nextPageToken": "t2",
            },
            {"items": [_item("v3", embed_segment_id("d", "s3"))]},
        ]
        assert fetch_remote_index(_FakeService("UU123", pages)) == {"s1": "v1", "s3": "v3"}

    def test_empty_when_no_uploads_playlist(self):
        assert fetch_remote_index(_FakeService(None, [])) == {}

    def test_errors_are_swallowed(self):
        class _Boom:
            def list(self, **kwargs):
                raise RuntimeError("boom")

        class _Service:
            def channels(self):
                return _Boom()

        assert fetch_remote_index(_Service()) == {}


class TestTitleSegmentId:
    def test_extract_from_bracketed_title(self):
        from tubetape.reconcile import extract_segment_id_from_title

        sid = "a1b2c3d4e5f67890"
        title = f"20240101-120000 - 20240101-122000 [{sid}]"
        assert extract_segment_id_from_title(title) == sid

    def test_extract_from_hyphenated_title(self):
        from tubetape.reconcile import extract_segment_id_from_title

        sid = "a1b2c3d4e5f67890"
        title = f"20240101-120000 - 20240101-122000-{sid}"
        assert extract_segment_id_from_title(title) == sid

    def test_extract_missing(self):
        from tubetape.reconcile import extract_segment_id_from_title

        assert extract_segment_id_from_title("just a regular title") is None
        assert extract_segment_id_from_title("") is None
        assert extract_segment_id_from_title(None) is None

    def test_fetch_remote_index_from_title(self):
        sid = "a1b2c3d4e5f67890"
        pages = [
            {
                "items": [
                    {
                        "snippet": {
                            "resourceId": {"videoId": "vid-123"},
                            "title": f"20240101 - 20240102 [{sid}]",
                            "description": "0:00 20240101-120000",
                        }
                    }
                ]
            }
        ]
        index = fetch_remote_index(_FakeService("UU123", pages))
        assert index.get(sid) == "vid-123"


class TestReconcileDatabase:
    def test_reconcile_direction_1_matches_missing_video_id(self, tmp_path):
        from tubetape.db import Database
        from tubetape.reconcile import reconcile_database

        db_path = tmp_path / "tubetape.json"
        db = Database(path=str(db_path))
        db.upsert_segment("seg_1", {
            "title": "Segment 1",
            "file_ids": ["f1"],
            "status": "sealed",
            "youtube_video_id": None,
        })
        db.save()

        stats = reconcile_database(db, {"seg_1": "yt_vid_1"}, {"yt_vid_1"})
        assert stats["matched"] == 1
        assert stats["deleted"] == 0
        assert db.segments["seg_1"]["youtube_video_id"] == "yt_vid_1"

    def test_reconcile_direction_2_detects_deleted_video(self, tmp_path):
        from tubetape.db import Database
        from tubetape.reconcile import reconcile_database

        db_path = tmp_path / "tubetape.json"
        db = Database(path=str(db_path))
        db.upsert_segment("seg_2", {
            "title": "Segment 2",
            "file_ids": ["f2"],
            "status": "sealed",
            "youtube_video_id": "yt_old_2",
            "previous_video_ids": [],
        })
        db.save()

        stats = reconcile_database(db, {}, {"yt_other_vid"})
        assert stats["matched"] == 0
        assert stats["deleted"] == 1
        assert db.segments["seg_2"]["youtube_video_id"] is None
        assert db.segments["seg_2"]["previous_video_ids"] == ["yt_old_2"]

    def test_reconcile_planned_segments(self, tmp_path):
        from tubetape.db import Database
        from tubetape.planner import Segment
        from tubetape.reconcile import reconcile_database

        db_path = tmp_path / "tubetape.json"
        db = Database(path=str(db_path))
        planned = [
            Segment(
                file_ids=["f1"],
                start_ts="2024-01-01T00:00:00Z",
                end_ts="2024-01-01T00:10:00Z",
                duration_seconds=600.0,
                segment_id="sid_planned_12345678",
                title="Planned 1",
            )
        ]

        stats = reconcile_database(db, {"sid_planned_12345678": "yt_planned_vid"}, {"yt_planned_vid"}, planned_segments=planned)
        assert stats["matched"] == 1
        assert "sid_planned_12345678" in db.segments
        assert db.segments["sid_planned_12345678"]["youtube_video_id"] == "yt_planned_vid"
