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
