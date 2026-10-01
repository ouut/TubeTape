from __future__ import annotations

import json
import os

import pytest

from tubetape import auth
from tubetape.uploader import (
    DAILY_QUOTA_DEFAULT,
    UPLOAD_QUOTA_UNITS,
    QuotaExceededError,
    QuotaTracker,
    Uploader,
    build_upload_body,
    is_quota_error,
)


class FakeQuotaError(Exception):
    def __init__(self, status=None, content=None):
        self.status = status
        self.content = content
        super().__init__(f"HTTP {status}")


class FakeInsert:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def execute(self):
        self.calls.append(1)
        if not self._responses:
            raise AssertionError("no more responses")
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp


class FakeVideos:
    def __init__(self, responses):
        self._responses = responses
        self.last_body = None

    def insert(self, **kwargs):
        self.last_body = kwargs.get("body")
        return FakeInsert(self._responses)


class FakePlaylistItems:
    def __init__(self):
        self.last_body = None

    def insert(self, **kwargs):
        self.last_body = kwargs.get("body")
        return FakeInsert([{"kind": "youtube#playlistItem", "id": "pl-item-1"}])


class FakeService:
    def __init__(self, responses):
        self._responses = responses
        self.videos_obj = FakeVideos(responses)
        self.playlist_items = FakePlaylistItems()

    def videos(self):
        return self.videos_obj

    def playlistItems(self):
        return self.playlist_items


class TestQuotaTracker:
    def test_remaining(self):
        q = QuotaTracker(daily_limit=10000, used=0)
        assert q.remaining == 10000

    def test_can_upload(self):
        q = QuotaTracker(daily_limit=1600, used=0)
        assert q.can_upload() is True

    def test_cannot_upload_when_insufficient(self):
        q = QuotaTracker(daily_limit=1600, used=1)
        assert q.can_upload() is False

    def test_consume(self):
        q = QuotaTracker(daily_limit=10000)
        assert q.consume() is True
        assert q.used == UPLOAD_QUOTA_UNITS
        assert q.remaining == 10000 - UPLOAD_QUOTA_UNITS

    def test_round_trip(self):
        q = QuotaTracker(daily_limit=10000, used=3200, date="2026-09-30")
        q2 = QuotaTracker.from_dict(q.to_dict())
        assert q2.daily_limit == 10000
        assert q2.used == 3200
        assert q2.date == "2026-09-30"

    def test_rollover_resets_on_new_day(self):
        q = QuotaTracker(daily_limit=10000, used=3200, date="2026-09-29")
        q.rollover(today="2026-09-30")
        assert q.used == 0
        assert q.date == "2026-09-30"

    def test_rollover_same_day_keeps_used(self):
        q = QuotaTracker(daily_limit=10000, used=3200, date="2026-09-30")
        q.rollover(today="2026-09-30")
        assert q.used == 3200

    def test_from_dict_empty(self):
        q = QuotaTracker.from_dict({})
        assert q.used == 0
        assert q.daily_limit == DAILY_QUOTA_DEFAULT


class TestBuildUploadBody:
    def test_all_fields(self):
        body = build_upload_body("t", "d", "unlisted", "22", False)
        assert body["snippet"]["title"] == "t"
        assert body["snippet"]["description"] == "d"
        assert body["snippet"]["categoryId"] == "22"
        assert body["status"]["privacyStatus"] == "unlisted"
        assert body["status"]["selfDeclaredMadeForKids"] is False
        assert body["status"]["selfCertification"]["selfDeclaredMadeForKids"] is False

    def test_defaults(self):
        body = build_upload_body("t", "d")
        assert body["status"]["privacyStatus"] == "private"
        assert body["snippet"]["categoryId"] == "22"


class TestIsQuotaError:
    def test_quota_reason(self):
        content = json.dumps(
            {"error": {"errors": [{"reason": "quotaExceeded"}]}}
        ).encode()
        assert is_quota_error(FakeQuotaError(status=403, content=content)) is True

    def test_other_403(self):
        content = json.dumps(
            {"error": {"errors": [{"reason": "forbidden"}]}}
        ).encode()
        assert is_quota_error(FakeQuotaError(status=403, content=content)) is False

    def test_generic(self):
        assert is_quota_error(FakeQuotaError(status=500)) is False


class TestUploader:
    def test_upload_returns_video_id(self, tmp_path):
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        service = FakeService([{"id": "vid-123"}])
        uploader = Uploader(service)
        video_id = uploader.upload(str(media), "title", "desc")
        assert video_id == "vid-123"
        assert uploader.quota.used == UPLOAD_QUOTA_UNITS

    def test_upload_body_passed(self, tmp_path):
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        service = FakeService([{"id": "vid-123"}])
        Uploader(service).upload(str(media), "title", "chapters...", privacy="unlisted")
        body = service.videos_obj.last_body
        assert body["snippet"]["title"] == "title"
        assert body["status"]["privacyStatus"] == "unlisted"

    def test_quota_exceeded_before_upload(self, tmp_path):
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        quota = QuotaTracker(daily_limit=UPLOAD_QUOTA_UNITS, used=1)
        uploader = Uploader(FakeService([{"id": "x"}]), quota=quota)
        with pytest.raises(QuotaExceededError):
            uploader.upload(str(media), "t", "d")

    def test_retry_then_success(self, tmp_path):
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        service = FakeService([RuntimeError("boom"), {"id": "vid-123"}])
        sleeps = []
        uploader = Uploader(service)
        video_id = uploader.upload(str(media), "t", "d", max_retries=5, base_delay=0, sleep=sleeps.append)
        assert video_id == "vid-123"
        assert sleeps == [0]  # one retry backoff

    def test_retry_exhausted(self, tmp_path):
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        service = FakeService([RuntimeError("boom")] * 3)
        with pytest.raises(RuntimeError):
            Uploader(service).upload(str(media), "t", "d", max_retries=2, base_delay=0, sleep=lambda s: None)

    def test_quota_error_during_execute_not_retried(self, tmp_path):
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        content = json.dumps({"error": {"errors": [{"reason": "quotaExceeded"}]}}).encode()
        service = FakeService([FakeQuotaError(status=403, content=content)])
        with pytest.raises(QuotaExceededError):
            Uploader(service).upload(str(media), "t", "d", base_delay=0, sleep=lambda s: None)

    def test_add_to_playlist(self, tmp_path):
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        service = FakeService([{"id": "vid-123"}])
        uploader = Uploader(service, playlist_id="PL123")
        uploader.upload(str(media), "t", "d")
        body = service.playlist_items.last_body
        assert body["snippet"]["playlistId"] == "PL123"
        assert body["snippet"]["resourceId"]["videoId"] == "vid-123"

    def test_no_playlist_when_unset(self, tmp_path):
        media = tmp_path / "v.mp4"
        media.write_bytes(b"x")
        service = FakeService([{"id": "vid-123"}])
        Uploader(service).upload(str(media), "t", "d")
        assert service.playlist_items.last_body is None


class TestAuth:
    def test_credentials_from_token_string(self):
        token = json.dumps({"token": "tok", "refresh_token": "ref", "client_id": "cid", "client_secret": "cs"})
        creds = auth.credentials_from_token_string(token)
        assert creds.token == "tok"
        assert creds.refresh_token == "ref"

    def test_credentials_from_bad_string(self):
        with pytest.raises(ValueError):
            auth.credentials_from_token_string("not json")

    def test_load_token_file(self, tmp_path):
        path = tmp_path / "token.json"
        path.write_text(json.dumps({"token": "tok", "refresh_token": "ref", "client_id": "cid", "client_secret": "cs"}))
        creds = auth.load_token_file(str(path))
        assert creds.token == "tok"

    def test_check_token_permissions(self, tmp_path):
        path = tmp_path / "token.json"
        path.write_text("{}")
        os.chmod(path, 0o600)
        assert auth.check_token_permissions(str(path)) is True
        os.chmod(path, 0o644)
        assert auth.check_token_permissions(str(path)) is False
