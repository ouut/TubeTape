from __future__ import annotations

import json
import time
import urllib.request

import pytest

from tubetape import web


def test_web_server_endpoints(tmp_path):
    log_file = tmp_path / "test.log"
    log_file.write_text("line 1: INFO test\nline 2: ERROR err\n", encoding="utf-8")

    server = web.WebServer(host="127.0.0.1", port=0, log_file=str(log_file))
    server.start()
    time.sleep(0.1)

    port = server._server.server_port
    base_url = f"http://127.0.0.1:{port}"

    try:
        # 1. Root page
        with urllib.request.urlopen(f"{base_url}/") as res:
            assert res.status == 200
            content = res.read().decode("utf-8")
            assert "TubeTape" in content

        # 2. Status API
        web.set_web_status("scanning", task="scanning files")
        with urllib.request.urlopen(f"{base_url}/api/status") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["status"] == "scanning"
            assert data["task"] == "scanning files"

        # 3. Logs API
        with urllib.request.urlopen(f"{base_url}/api/logs?offset=0") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert "line 1: INFO test" in data["content"]
            assert data["offset"] > 0

        # 4. OAuth code intercept
        with urllib.request.urlopen(f"{base_url}/?code=auth12345") as res:
            assert res.status == 200
            html = res.read().decode("utf-8")
            assert "授权成功" in html
        code = web.get_auth_code(timeout=1.0)
        assert code == "auth12345"

    finally:
        server.stop()


class _MockSession:
    def __init__(self, creds=None, raise_on_exchange=False):
        self.auth_url = "https://accounts.google.com/o/oauth2/auth?test=1"
        self.credentials = creds or object()
        self.exchanged = []
        self.raise_on_exchange = raise_on_exchange

    def exchange(self, code_or_url: str):
        if self.raise_on_exchange:
            raise ValueError("invalid code")
        self.exchanged.append(code_or_url)
        return self.credentials


def test_web_server_oauth_session_submit(tmp_path):
    server = web.WebServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.1)

    port = server._server.server_port
    base_url = f"http://127.0.0.1:{port}"
    mock_session = _MockSession()

    try:
        web.set_web_oauth_session(mock_session)

        # 1. Status indicates auth required
        with urllib.request.urlopen(f"{base_url}/api/status") as res:
            data = json.loads(res.read().decode("utf-8"))
            assert data["auth_required"] is True
            assert data["auth_url"] == mock_session.auth_url
            assert data["status"] == "waiting_auth"

        # 2. POST /api/auth/submit
        req_data = json.dumps({"url": "http://localhost:8080/?code=code_via_post"}).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/api/auth/submit",
            data=req_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as res:
            assert res.status == 200
            resp_json = json.loads(res.read().decode("utf-8"))
            assert resp_json["ok"] is True

        assert mock_session.exchanged == ["http://localhost:8080/?code=code_via_post"]

        # 3. wait_for_auth completes and returns creds
        creds = web.wait_for_auth(timeout=2.0)
        assert creds is mock_session.credentials

        # 4. Status now indicates auth not required
        with urllib.request.urlopen(f"{base_url}/api/status") as res:
            data = json.loads(res.read().decode("utf-8"))
            assert data["auth_required"] is False
            assert data["auth_url"] is None
    finally:
        web.clear_web_oauth_session()
        server.stop()


def test_web_server_oauth_session_get_callback(tmp_path):
    server = web.WebServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.1)

    port = server._server.server_port
    base_url = f"http://127.0.0.1:{port}"
    mock_session = _MockSession()

    try:
        web.set_web_oauth_session(mock_session)

        # GET /?code=code_via_get
        with urllib.request.urlopen(f"{base_url}/?code=code_via_get") as res:
            assert res.status == 200
            html = res.read().decode("utf-8")
            assert "授权成功" in html

        assert mock_session.exchanged == ["code_via_get"]
        creds = web.wait_for_auth(timeout=1.0)
        assert creds is mock_session.credentials
    finally:
        web.clear_web_oauth_session()
        server.stop()


def test_web_server_submit_error(tmp_path):
    server = web.WebServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.1)

    port = server._server.server_port
    base_url = f"http://127.0.0.1:{port}"
    mock_session = _MockSession(raise_on_exchange=True)

    try:
        web.set_web_oauth_session(mock_session)

        req_data = json.dumps({"url": "http://localhost:8080/?bad=1"}).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/api/auth/submit",
            data=req_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400
    finally:
        web.clear_web_oauth_session()
        server.stop()


def test_wait_for_auth_timeout():
    web._server_state["auth_event"].clear()
    web._server_state["oauth_session"] = object()
    with pytest.raises(TimeoutError, match="超时"):
        web.wait_for_auth(timeout=0.05)
    web.clear_web_oauth_session()


def test_web_server_dashboard_and_logs_endpoints(tmp_path):
    log_file = tmp_path / "test.log"
    log_file.write_text("INFO dashboard test line\n", encoding="utf-8")
    server = web.WebServer(host="127.0.0.1", port=0, log_file=str(log_file))
    server.start()
    time.sleep(0.1)
    port = server._server.server_port
    base_url = f"http://127.0.0.1:{port}"

    try:
        # /log
        with urllib.request.urlopen(f"{base_url}/log") as res:
            assert res.status == 200
            html = res.read().decode("utf-8")
            assert "运行控制台与仪表盘" in html
            assert "时间线分段列表" in html
            assert "实时运行日志" in html

        # /logs
        with urllib.request.urlopen(f"{base_url}/logs") as res:
            assert res.status == 200

        # /api/dashboard
        web.update_web_scanner(is_scanning=True, count=42, current="test.jpg")
        web.update_web_transcode(segment_id="seg1", title="Segment 1", done=5, total=10, current_file="a.jpg")
        with urllib.request.urlopen(f"{base_url}/api/dashboard") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["scanner"]["is_scanning"] is True
            assert data["scanner"]["count"] == 42
            assert data["transcode"]["title"] == "Segment 1"
            assert "stats" in data
            assert "segments" in data
    finally:
        web.update_web_scanner(is_scanning=False, count=0)
        web.finish_web_segment("seg1")
        server.stop()


def test_web_server_media_api_and_streaming(tmp_path):
    import os
    from PIL import Image
    from tubetape.db import Database

    media_dir = tmp_path / "media"
    media_dir.mkdir()
    img_path = media_dir / "photo.jpg"
    img = Image.new("RGB", (100, 100), color="red")
    img.save(img_path, "JPEG")

    video_path = media_dir / "video.mp4"
    video_bytes = b"fake video content header and body chunk 1234567890" * 100
    video_path.write_bytes(video_bytes)

    db_path = tmp_path / "db.json"
    db = Database(path=str(db_path))
    db.upsert_file("img_id", {
        "path": "photo.jpg",
        "name": "photo.jpg",
        "type": "image",
        "captured_at_utc": "2024-05-01T12:00:00Z",
        "resolution": "100x100",
        "size_bytes": os.path.getsize(img_path),
    })
    db.upsert_file("vid_id", {
        "path": "video.mp4",
        "name": "video.mp4",
        "type": "video",
        "captured_at_utc": "2024-05-02T15:00:00Z",
        "duration_seconds": 12.0,
        "size_bytes": len(video_bytes),
    })
    db.save()

    web.set_web_context(media_dir=str(media_dir), db=db, db_path=str(db_path), keep_segments=3)
    server = web.WebServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.1)
    port = server._server.server_port
    base_url = f"http://127.0.0.1:{port}"

    try:
        # /api/media/summary
        with urllib.request.urlopen(f"{base_url}/api/media/summary") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["total"] == 2
            assert len(data["date_groups"]) > 0

        # /api/media/items
        with urllib.request.urlopen(f"{base_url}/api/media/items?offset=0&limit=10") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["total"] == 2
            assert len(data["items"]) == 2
            assert data["items"][0]["id"] == "img_id"

        # /api/media/view?id=img_id
        with urllib.request.urlopen(f"{base_url}/api/media/view?id=img_id") as res:
            assert res.status == 200
            assert res.headers.get("Content-Type") == "image/jpeg"
            content = res.read()
            assert len(content) > 0

        # /api/media/stream?id=vid_id (Range request)
        req = urllib.request.Request(f"{base_url}/api/media/stream?id=vid_id")
        req.add_header("Range", "bytes=10-49")
        with urllib.request.urlopen(req) as res:
            assert res.status == 206
            assert res.headers.get("Content-Range") == f"bytes 10-49/{len(video_bytes)}"
            assert res.headers.get("Content-Length") == "40"
            chunk = res.read()
            assert chunk == video_bytes[10:50]
    finally:
        server.stop()


