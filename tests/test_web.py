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

        web.update_web_scanner(is_scanning=True, count=42, current="test.jpg")
        web.update_web_transcode(segment_id="seg1", title="Segment 1", done=5, total=10, current_file="a.jpg", file_progress=50.0, fps=30.0, speed="1.2x")
        with urllib.request.urlopen(f"{base_url}/api/dashboard") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["scanner"]["is_scanning"] is True
            assert data["scanner"]["count"] == 42
            assert data["transcode"]["title"] == "Segment 1"
            assert data["transcode"]["file_progress"] == 50.0
            assert data["transcode"]["fps"] == 30.0
            assert data["transcode"]["speed"] == "1.2x"
            assert "stats" in data
            assert "uploaded_files_count" in data["stats"]
            assert "pending_files_count" in data["stats"]
            assert "overlap_files" in data["stats"]
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


class _FakeUploader:
    def __init__(self):
        self.deleted_videos = []
        self.uploaded_videos = []

    def upload(self, media_path, title, description, privacy="private", **kwargs):
        vid = f"yt_{title[:8]}"
        self.uploaded_videos.append(vid)
        return vid

    def verify(self, video_id):
        pass

    def delete_video(self, video_id):
        self.deleted_videos.append(video_id)


def test_web_server_config_and_coordinator_actions(tmp_path):
    from tubetape.coordinator import AppCoordinator
    from tubetape.cli import parse_args
    from tubetape.db import Database

    from conftest import make_png
    from tubetape.scanner import file_sha256
    make_png(tmp_path / "img.png")
    real_fid = file_sha256(str(tmp_path / "img.png"))

    db_path = tmp_path / "db.json"
    db = Database(path=str(db_path))
    db.upsert_segment("seg1", {
        "title": "20240101 - 20240102 [seg1]",
        "file_ids": [real_fid],
        "range": ["2024-01-01T00:00:00Z", "2024-01-02T00:00:00Z"],
        "duration_seconds": 60.0,
        "youtube_video_id": "yt_existing_123",
        "previous_video_ids": [],
        "status": "sealed",
    })
    db.upsert_file(real_fid, {
        "path": "img.png",
        "name": "img.png",
        "type": "image",
        "captured_at_utc": "2024-01-01T00:00:00Z",
        "duration_seconds": 3.0,
        "size_bytes": 10,
    })

    args = parse_args(["--input", str(tmp_path), "--db", str(db_path)])
    fake_uploader = _FakeUploader()
    coord = AppCoordinator(args, db=db, uploader=fake_uploader)

    web.set_app_coordinator(coord)
    web.set_web_context(media_dir=str(tmp_path), db=db, db_path=str(db_path), keep_segments=2)

    server = web.WebServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.1)
    port = server._server.server_port
    base_url = f"http://127.0.0.1:{port}"

    try:
        # 1. GET /api/config
        with urllib.request.urlopen(f"{base_url}/api/config") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert "config" in data
            assert data["config"]["crf"] == 18
            assert "segment_duration" in data["fingerprint_params"]

        # 2. POST /api/config
        update_data = json.dumps({"crf": 22, "keep_segments": 5, "no_upload": True}).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/api/config",
            data=update_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["ok"] is True
            assert data["config"]["crf"] == 22
            assert data["config"]["keep_segments"] == 5
            assert data["config"]["no_upload"] is True

        assert coord.args.crf == 22
        assert coord.args.keep_segments == 5
        assert coord.args.no_upload is True
        # Verify saved to tubetape.json database directly
        from tubetape.db import Database
        saved_db = Database.load(str(db_path))
        assert saved_db.config["crf"] == 22
        assert saved_db.config["keep_segments"] == 5
        assert saved_db.config["no_upload"] is True
        assert not (tmp_path / "config.json").exists()

        # 3. POST /api/config with invalid value fails
        bad_req = urllib.request.Request(
            f"{base_url}/api/config",
            data=json.dumps({"crf": 999}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(bad_req)
        assert exc_info.value.code == 400

        # Wait for any in-flight background task to complete before modifying segments
        for _ in range(50):
            if coord.current_task is None:
                break
            time.sleep(0.05)

        # 4. POST /api/segment/delete_youtube
        del_req = urllib.request.Request(
            f"{base_url}/api/segment/delete_youtube?id=seg1",
            data=b"",
            method="POST",
        )
        with urllib.request.urlopen(del_req) as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["ok"] is True

        assert "yt_existing_123" in fake_uploader.deleted_videos
        assert db.get_segment("seg1")["youtube_video_id"] is None
        assert "yt_existing_123" in db.get_segment("seg1")["previous_video_ids"]

        # 5. POST /api/segment/upload when file is missing -> fails
        up_req = urllib.request.Request(
            f"{base_url}/api/segment/upload?id=seg1",
            data=b"",
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(up_req)
        assert exc_info.value.code == 400

        # Create segment file in uploaded_segments/ and retry upload
        seg_dir = tmp_path / "uploaded_segments"
        seg_dir.mkdir(exist_ok=True)
        seg_file = seg_dir / "20240101 - 20240102 [seg1].mp4"
        seg_file.write_bytes(b"dummy_video_bytes")

        up_req2 = urllib.request.Request(
            f"{base_url}/api/segment/upload?id=seg1",
            data=b"",
            method="POST",
        )
        with urllib.request.urlopen(up_req2) as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["ok"] is True

        # 6. GET /api/dashboard verifies has_local_file and stats
        with urllib.request.urlopen(f"{base_url}/api/dashboard") as res:
            assert res.status == 200
            d = json.loads(res.read().decode("utf-8"))
            assert d["stats"]["no_upload"] is True
            assert d["segments"][0]["has_local_file"] is True

        # Wait for any in-flight background task to complete before triggering scan
        for _ in range(50):
            if coord.current_task is None:
                break
            time.sleep(0.05)

        # 7. POST /api/scan/start
        req_scan = urllib.request.Request(f"{base_url}/api/scan/start", data=b"", method="POST")
        with urllib.request.urlopen(req_scan) as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["ok"] is True

        # 8. GET /api/segments
        with urllib.request.urlopen(f"{base_url}/api/segments") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert len(data["segments"]) == 1
            assert data["segments"][0]["id"] == "seg1"

        # 9. GET /api/segment/items?id=seg1
        with urllib.request.urlopen(f"{base_url}/api/segment/items?id=seg1") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["ok"] is True
            assert len(data["items"]) == 1
            assert data["items"][0]["id"] == real_fid
            assert "img.png" in data["items"][0]["abs_path"]

        # 10. GET /getbytime/seg1/0/1 (HTML with real file path)
        with urllib.request.urlopen(f"{base_url}/getbytime/seg1/0/1") as res:
            assert res.status == 200
            html = res.read().decode("utf-8")
            assert "真实磁盘物理路径" in html
            assert "img.png" in html

        # 11. GET /getbytime/seg1/0/1?format=json
        with urllib.request.urlopen(f"{base_url}/getbytime/seg1/0/1?format=json") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["ok"] is True
            assert data["file_id"] == real_fid
            assert data["path"] == "img.png"
            assert "img.png" in data["abs_path"]

        # 12. GET /getbytime/seg1/10/0 (Out of bounds)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{base_url}/getbytime/seg1/10/0")
        assert exc_info.value.code == 404

    finally:
        web.set_app_coordinator(None)
        server.stop()


def test_log_dashboard_html_and_js_syntax():
    import re
    import shutil
    import subprocess

    # 1. HTML contains dashboard and log controls
    assert "TubeTape" in web._LOG_DASHBOARD_HTML
    assert "log-box" in web._LOG_DASHBOARD_HTML
    assert "pollDashboard" in web._LOG_DASHBOARD_HTML
    assert "pollLogs" in web._LOG_DASHBOARD_HTML

    # 2. Extract JS from script tag and check syntax
    m = re.search(r"<script>(.*?)</script>", web._LOG_DASHBOARD_HTML, re.DOTALL)
    assert m is not None, "Script tag missing in _LOG_DASHBOARD_HTML"
    js_code = m.group(1)

    node_bin = shutil.which("node")
    if node_bin:
        res = subprocess.run([node_bin, "--check"], input=js_code, capture_output=True, text=True)
        assert res.returncode == 0, f"JS SyntaxError in _LOG_DASHBOARD_HTML: {res.stderr}"


def test_dashboard_scanning_status_and_logs(tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text("2026-10-09 INFO scanning directory: /data\n", encoding="utf-8")

    server = web.WebServer(host="127.0.0.1", port=0, log_file=str(log_file))
    server.start()
    time.sleep(0.1)

    port = server._server.server_port
    base_url = f"http://127.0.0.1:{port}"

    try:
        # Simulate active scanning state
        web.set_web_status("scanning", "正在扫描 /data ...")
        web.update_web_scanner(is_scanning=True, count=128, current="family/vacation.jpg")

        # 1. GET /log HTML
        with urllib.request.urlopen(f"{base_url}/log") as res:
            assert res.status == 200
            html = res.read().decode("utf-8")
            assert "实时运行日志" in html

        # 2. GET /api/dashboard during scan
        with urllib.request.urlopen(f"{base_url}/api/dashboard") as res:
            assert res.status == 200
            d = json.loads(res.read().decode("utf-8"))
            assert d["status"] == "scanning"
            assert d["task"] == "正在扫描 /data ..."
            assert d["scanner"]["is_scanning"] is True
            assert d["scanner"]["count"] == 128
            assert d["scanner"]["current"] == "family/vacation.jpg"

        # 3. GET /api/logs incremental
        with urllib.request.urlopen(f"{base_url}/api/logs?offset=0") as res:
            assert res.status == 200
            l1 = json.loads(res.read().decode("utf-8"))
            assert "scanning directory: /data" in l1["content"]
            offset = l1["offset"]

        # Append new log line
        with open(log_file, "a", encoding="utf-8") as f:
            f.write("2026-10-09 INFO scan [1/1] family/vacation.jpg (image, 2.5 MB)\n")

        with urllib.request.urlopen(f"{base_url}/api/logs?offset={offset}") as res:
            assert res.status == 200
            l2 = json.loads(res.read().decode("utf-8"))
            assert "scan [1/1] family/vacation.jpg" in l2["content"]

    finally:
        web.set_web_status("idle", "")
        web.update_web_scanner(is_scanning=False, count=0)
        server.stop()


def test_gallery_planned_segments_and_items(tmp_path):
    from tubetape.db import Database
    from tubetape.planner import Segment

    db_path = tmp_path / "tubetape.json"
    db = Database(path=str(db_path))
    db.upsert_file("f1", {
        "path": "img1.png",
        "name": "img1.png",
        "type": "image",
        "captured_at_utc": "2024-01-01T12:00:00Z",
        "duration_seconds": 3.0,
        "size_bytes": 1024,
    })
    db.upsert_file("f2", {
        "path": "img2.png",
        "name": "img2.png",
        "type": "image",
        "captured_at_utc": "2024-01-01T12:05:00Z",
        "duration_seconds": 3.0,
        "size_bytes": 2048,
    })
    db.save()

    # Create dummy media files
    (tmp_path / "img1.png").write_bytes(b"dummy1")
    (tmp_path / "img2.png").write_bytes(b"dummy2")

    server = web.WebServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.1)
    port = server._server.server_port
    base_url = f"http://127.0.0.1:{port}"

    try:
        web.set_web_context(media_dir=str(tmp_path), db=db, db_path=str(db_path))

        # Planned segment in memory (db.segments is empty)
        plan_seg = Segment(
            segment_id="plan_seg_1234567890abcdef",
            title="20240101-120000 - 20240101-120500 [plan_seg]",
            start_ts="2024-01-01T12:00:00Z",
            end_ts="2024-01-01T12:05:00Z",
            duration_seconds=6.0,
            file_ids=["f1", "f2"],
        )
        web.set_web_planned_segments([plan_seg])

        # 1. GET /api/segments should return the planned segment
        with urllib.request.urlopen(f"{base_url}/api/segments") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            segs = data["segments"]
            assert len(segs) == 1
            assert segs[0]["id"] == "plan_seg_1234567890abcdef"
            assert segs[0]["title"] == "20240101-120000 - 20240101-120500 [plan_seg]"
            assert segs[0]["file_count"] == 2
            assert segs[0]["status"] == "pending"

        # 2. GET /api/segment/items?id=plan_seg_1234567890abcdef should return files
        with urllib.request.urlopen(f"{base_url}/api/segment/items?id=plan_seg_1234567890abcdef") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["ok"] is True
            assert data["segment_id"] == "plan_seg_1234567890abcdef"
            assert len(data["items"]) == 2
            assert data["items"][0]["id"] == "f1"
            assert data["items"][1]["id"] == "f2"

        # Prefix match should also work
        with urllib.request.urlopen(f"{base_url}/api/segment/items?id=plan_seg_1234") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["ok"] is True
            assert len(data["items"]) == 2

        # 3. Transcoding active state changes status to building
        web.update_web_transcode(
            segment_id="plan_seg_1234567890abcdef",
            title=plan_seg.title,
            done=1,
            total=2,
            current_file="img1.png",
        )
        with urllib.request.urlopen(f"{base_url}/api/segments") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["segments"][0]["status"] == "building"

    finally:
        web.set_web_planned_segments([])
        web.finish_web_segment("plan_seg_1234567890abcdef")
        server.stop()


def test_gallery_dynamic_plan_fallback(tmp_path):
    from tubetape.db import Database

    db_path = tmp_path / "tubetape.json"
    db = Database(path=str(db_path))
    db.upsert_file("f1", {
        "path": "photo1.jpg",
        "name": "photo1.jpg",
        "type": "image",
        "captured_at_utc": "2024-05-01T10:00:00Z",
        "duration_seconds": 3.0,
        "size_bytes": 1024,
    })
    db.save()

    server = web.WebServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.1)
    port = server._server.server_port
    base_url = f"http://127.0.0.1:{port}"

    try:
        web.set_web_context(media_dir=str(tmp_path), db=db, db_path=str(db_path))
        web.set_web_planned_segments([])

        # Both db.segments and planned_segments are empty!
        # /api/segments should dynamically plan from db.files
        with urllib.request.urlopen(f"{base_url}/api/segments") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            segs = data["segments"]
            assert len(segs) >= 1
            dyn_id = segs[0]["id"]
            assert segs[0]["file_count"] == 1

        # /api/segment/items should also resolve the dynamically planned segment
        with urllib.request.urlopen(f"{base_url}/api/segment/items?id={dyn_id}") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["ok"] is True
            assert len(data["items"]) == 1
            assert data["items"][0]["id"] == "f1"

    finally:
        server.stop()


def test_web_upload_queue_and_progress_overlay(tmp_path):
    from tubetape.db import Database

    db_path = tmp_path / "tubetape.json"
    db = Database(path=str(db_path))
    db.upsert_segment("seg_1", {
        "title": "Segment 1",
        "file_ids": ["f1"],
        "duration_seconds": 120.0,
        "status": "sealed",
        "output_path": str(tmp_path / "seg1.mp4"),
        "youtube_video_id": None,
    })
    db.upsert_segment("seg_2", {
        "title": "Segment 2",
        "file_ids": ["f2"],
        "duration_seconds": 150.0,
        "status": "sealed",
        "output_path": str(tmp_path / "seg2.mp4"),
        "youtube_video_id": None,
    })
    db.save()

    server = web.WebServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.1)
    port = server._server.server_port
    base_url = f"http://127.0.0.1:{port}"

    try:
        web.set_web_context(media_dir=str(tmp_path), db=db, db_path=str(db_path))

        # 1. Initially both are sealed without youtube_video_id -> is_pending_upload
        with urllib.request.urlopen(f"{base_url}/api/dashboard") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            segs = {s["segment_id"]: s for s in data["segments"]}
            assert segs["seg_1"]["is_pending_upload"] is True
            assert segs["seg_1"]["is_upload_queued"] is False
            assert segs["seg_1"]["upload_progress"] is None

        # 2. Set upload queue and update active upload progress
        web.set_upload_queue(["seg_1", "seg_2"])
        web.update_web_upload("seg_1", title="Segment 1", percent=42.5, uploaded_bytes=4250, total_bytes=10000, speed="1.2 MB/s")

        with urllib.request.urlopen(f"{base_url}/api/dashboard") as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["upload"]["segment_id"] == "seg_1"
            assert data["upload"]["percent"] == 42.5
            assert data["upload"]["speed"] == "1.2 MB/s"
            assert data["upload_queue"] == ["seg_1", "seg_2"]

            segs = {s["segment_id"]: s for s in data["segments"]}
            assert segs["seg_1"]["status"] == "uploading"
            assert segs["seg_1"]["upload_progress"]["percent"] == 42.5
            assert segs["seg_2"]["is_upload_queued"] is True

        # 3. Finish seg_1
        web.finish_web_segment("seg_1", youtube_video_id="yt_vid_seg_1")
        assert web.get_upload_queue() == ["seg_2"]
        assert web._server_state["upload"]["segment_id"] is None

    finally:
        web.clear_web_upload()
        web.set_upload_queue([])
        server.stop()


def test_web_api_sync_youtube(tmp_path):
    from unittest.mock import MagicMock
    from tubetape.coordinator import AppCoordinator
    from tubetape.db import Database

    db_path = tmp_path / "tubetape.json"
    db = Database(path=str(db_path))
    db.upsert_segment("seg_s1", {
        "title": "Segment Sync 1",
        "file_ids": ["f1"],
        "status": "sealed",
        "youtube_video_id": None,
    })
    db.save()

    mock_uploader = MagicMock()
    mock_uploader.service = MagicMock()
    coord = AppCoordinator(args=MagicMock(db=str(db_path), input=str(tmp_path)), db=db, uploader=mock_uploader)

    from tubetape import coordinator
    orig_fetch = coordinator.fetch_remote_catalog
    server = web.WebServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.1)
    port = server._server.server_port
    base_url = f"http://127.0.0.1:{port}"

    try:
        coordinator.fetch_remote_catalog = lambda s: ({"seg_s1": "yt_remote_s1"}, {"yt_remote_s1"}, True)
        web.set_app_coordinator(coord)
        web.set_web_context(media_dir=str(tmp_path), db=db, db_path=str(db_path))

        req = urllib.request.Request(
            f"{base_url}/api/sync_youtube",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["ok"] is True
            assert data["stats"]["matched"] == 1
            assert db.segments["seg_s1"]["youtube_video_id"] == "yt_remote_s1"
    finally:
        coordinator.fetch_remote_catalog = orig_fetch
        server.stop()


def test_web_multi_page_routes(tmp_path):
    server = web.WebServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.1)
    port = server._server.server_port
    base_url = f"http://127.0.0.1:{port}"

    try:
        pages = [
            ("/", "控制台"),
            ("/view", "视界画廊"),
            ("/segments", "分片管理"),
            ("/logs", "运行日志"),
            ("/settings", "系统配置"),
        ]
        for route, expected_text in pages:
            with urllib.request.urlopen(f"{base_url}{route}") as res:
                assert res.status == 200
                html = res.read().decode("utf-8")
                assert expected_text in html
                assert "TubeTape" in html
        # Common navbar links on standard pages
        for route in ["/", "/segments", "/logs", "/settings"]:
            with urllib.request.urlopen(f"{base_url}{route}") as res:
                html = res.read().decode("utf-8")
                assert 'href="/view"' in html
                assert 'href="/segments"' in html
                assert 'href="/logs"' in html
                assert 'href="/settings"' in html
    finally:
        server.stop()


def test_web_segment_toggle_and_include(tmp_path):
    from unittest.mock import MagicMock
    from tubetape.coordinator import AppCoordinator
    from tubetape.db import Database

    db_path = tmp_path / "tubetape.json"
    db = Database(path=str(db_path))
    db.upsert_file("f1", {
        "path": "img1.png",
        "file_type": "image",
        "captured_at_utc": "2026-01-01 10:05:00",
        "size": 500,
        "segment_id": "seg_test",
    })
    db.upsert_file("f2", {
        "path": "img2.png",
        "file_type": "image",
        "captured_at_utc": "2026-01-01 10:10:00",
        "size": 500,
        "segment_id": None,
    })
    db.upsert_segment("seg_test", {
        "title": "Test Segment",
        "file_ids": ["f1"],
        "status": "planned",
        "youtube_video_id": None,
        "range": ["2026-01-01 10:00:00", "2026-01-01 11:00:00"],
    })
    db.save()

    from tubetape import cli
    test_args = cli.parse_args(["--input", str(tmp_path), "--db", str(db_path), "--no-upload", "--no-watch"])
    coord = AppCoordinator(args=test_args, db=db)
    coord.restart_pipeline = MagicMock(return_value=(True, "ok"))
    server = web.WebServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.1)
    port = server._server.server_port
    base_url = f"http://127.0.0.1:{port}"

    try:
        web.set_app_coordinator(coord)
        web.set_web_context(media_dir=str(tmp_path), db=db, db_path=str(db_path))

        view_data = coord.get_segments_view_data()
        assert len(view_data) == 1
        assert view_data[0]["segment_id"] == "seg_test"
        assert len(view_data[0]["candidate_files"]) == 1
        assert view_data[0]["candidate_files"][0]["file_id"] == "f2"

        # Toggle f1 excluded
        req_data = json.dumps({"segment_id": "seg_test", "file_id": "f1", "excluded": True}).encode("utf-8")
        req = urllib.request.Request(f"{base_url}/api/segment/toggle_file", data=req_data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["ok"] is True
            assert "f1" in db.segments["seg_test"]["excluded_file_ids"]

        # Include candidate f2
        req_data = json.dumps({"segment_id": "seg_test", "file_id": "f2"}).encode("utf-8")
        req = urllib.request.Request(f"{base_url}/api/segment/include_file", data=req_data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["ok"] is True
            assert "f2" in db.segments["seg_test"]["file_ids"]

        # Check config API and config apply
        with urllib.request.urlopen(f"{base_url}/api/config") as res:
            assert res.status == 200
            cfg_data = json.loads(res.read().decode("utf-8"))
            assert "config" in cfg_data

        apply_data = json.dumps({"updates": {"crf": 22}}).encode("utf-8")
        req = urllib.request.Request(f"{base_url}/api/config/apply", data=apply_data, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as res:
            assert res.status == 200
            applied = json.loads(res.read().decode("utf-8"))
            assert applied["ok"] is True
            assert coord.args.crf == 22
            assert db.config.get("crf") == 22
    finally:
        server.stop()


def test_segment_delete_local_and_split_and_logs_pagination(tmp_path):
    from tubetape.coordinator import AppCoordinator
    from tubetape.db import Database
    from tubetape import cli

    db_path = tmp_path / "db.json"
    seg_dir = tmp_path / "uploaded_segments"
    seg_dir.mkdir(exist_ok=True)

    db = Database(path=str(db_path))
    db.upsert_file("f_a", {
        "path": "img_a.png", "name": "img_a.png", "type": "video",
        "duration_seconds": 15.0, "captured_at_utc": "2024-01-01T10:00:00Z"
    })
    db.upsert_file("f_b", {
        "path": "img_b.png", "name": "img_b.png", "type": "video",
        "duration_seconds": 15.0, "captured_at_utc": "2024-01-01T10:10:00Z"
    })
    db.upsert_segment("seg_oversized", {
        "title": "20240101_100000[seg_oversized]",
        "file_ids": ["f_a", "f_b"],
        "range": ["2024-01-01T10:00:00Z", "2024-01-01T10:10:00Z"],
        "duration_seconds": 30.0,
        "youtube_video_id": None,
        "status": "pending",
    })
    db.save()

    # Create dummy local segment file
    seg_file = seg_dir / "20240101_100000[seg_oversized].mp4"
    seg_file.write_bytes(b"test_local_mp4_bytes")

    test_args = cli.parse_args([
        "--input", str(tmp_path),
        "--db", str(db_path),
        "--segments-dir", str(seg_dir),
        "--segment-duration", "20s",
        "--no-upload",
        "--no-watch",
    ])
    coord = AppCoordinator(args=test_args, db=db)
    server = web.WebServer(host="127.0.0.1", port=0)
    server.start()
    time.sleep(0.1)
    port = server._server.server_port
    base_url = f"http://127.0.0.1:{port}"
    web.set_app_coordinator(coord)
    web.set_web_context(media_dir=str(tmp_path), db=db, db_path=str(db_path))

    try:
        # 1. Verify logs page has pagination elements
        with urllib.request.urlopen(f"{base_url}/logs") as res:
            assert res.status == 200
            html = res.read().decode("utf-8")
            assert "page-size-select" in html
            assert "btn-first" in html
            assert "btn-next" in html
            assert "跟随最新" in html

        # 2. Test POST /api/segment/delete_local
        assert seg_file.is_file()
        req_del = urllib.request.Request(
            f"{base_url}/api/segment/delete_local?id=seg_oversized",
            data=b"",
            method="POST",
        )
        with urllib.request.urlopen(req_del) as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["ok"] is True
            assert not seg_file.is_file()

        # 3. Test POST /api/segment/split
        req_split = urllib.request.Request(
            f"{base_url}/api/segment/split?id=seg_oversized",
            data=b"",
            method="POST",
        )
        with urllib.request.urlopen(req_split) as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["ok"] is True
            assert len(data["new_segment_ids"]) == 2
            assert "seg_oversized" not in db.segments
            for nid in data["new_segment_ids"]:
                assert nid in db.segments
    finally:
        server.stop()


def test_segment_rebuild_endpoint(tmp_path, monkeypatch):
    import json
    import urllib.request
    from tubetape.coordinator import AppCoordinator
    from tubetape.db import Database
    from tubetape.web import WebServer, set_app_coordinator

    db_file = tmp_path / "db.json"
    db = Database(path=str(db_file))
    db.segments["seg_rb"] = {
        "title": "20240101 Test",
        "file_ids": [],
        "status": "sealed",
    }
    db.save()

    class FakeArgs:
        input = str(tmp_path)
        segments_dir = str(tmp_path / "segments")
        keep_segments = 0
        no_upload = True

    coord = AppCoordinator(FakeArgs(), db=db)
    rebuild_called = []
    monkeypatch.setattr(coord, "rebuild_segment", lambda seg_id: rebuild_called.append(seg_id) or (True, "rebuild started"))

    server = WebServer(host="127.0.0.1", port=0, db_path=str(db_file))
    server.start()
    set_app_coordinator(coord)
    base_url = f"http://127.0.0.1:{server.port}"

    try:
        req = urllib.request.Request(
            f"{base_url}/api/segment/rebuild?id=seg_rb",
            data=b"",
            method="POST",
        )
        with urllib.request.urlopen(req) as res:
            assert res.status == 200
            data = json.loads(res.read().decode("utf-8"))
            assert data["ok"] is True
            assert rebuild_called == ["seg_rb"]
    finally:
        server.stop()


def test_client_disconnect_handling():
    from tubetape.web import _RequestHandler, _TubeTapeHTTPServer

    class FakeWFile:
        def write(self, data):
            raise ConnectionAbortedError(10053, "An established connection was aborted")

    class FakeHandler:
        def __init__(self):
            self.wfile = FakeWFile()
        def send_response(self, code):
            pass
        def send_header(self, k, v):
            pass
        def end_headers(self):
            pass

    handler = FakeHandler()
    # Should not raise ConnectionAbortedError
    _RequestHandler._send_json(handler, 200, {"status": "ok"})
    _RequestHandler._send_html(handler, 200, "<html></html>")

    # Test server handle_error with ConnectionAbortedError
    import sys
    server = _TubeTapeHTTPServer.__new__(_TubeTapeHTTPServer)
    try:
        raise ConnectionAbortedError(10053, "aborted")
    except ConnectionAbortedError:
        # Should cleanly return without calling super().handle_error
        server.handle_error(None, ("127.0.0.1", 12345))


def test_all_html_templates_js_syntax():
    import re
    import shutil
    import subprocess
    from tubetape.web.dashboard import render_dashboard_html
    from tubetape.web.view import render_view_html
    from tubetape.web.segments import render_segments_html
    from tubetape.web.logs import render_logs_html
    from tubetape.web.settings import render_settings_html

    pages = [
        ("dashboard", render_dashboard_html()),
        ("view", render_view_html()),
        ("segments", render_segments_html()),
        ("logs", render_logs_html()),
        ("settings", render_settings_html()),
    ]

    has_node = shutil.which("node") is not None
    for name, html in pages:
        assert len(html) > 100
        scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        assert len(scripts) >= 1
        if has_node:
            for i, script_content in enumerate(scripts):
                res = subprocess.run(["node", "-c", "-"], input=script_content.encode("utf-8"), capture_output=True)
                assert res.returncode == 0, f"JS Syntax error in {name} script {i}: {res.stderr.decode('utf-8')}"










