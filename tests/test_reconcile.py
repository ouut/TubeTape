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
