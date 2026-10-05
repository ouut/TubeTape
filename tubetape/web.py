"""Lightweight HTTP server for live log viewing and OAuth callbacks.

Runs in a daemon thread so it never blocks or prevents clean shutdown.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_logger = logging.getLogger("tubetape.web")

# Shared state updated by Reporter or CLI
_server_state = {
    "status": "idle",
    "task": "",
    "log_file": None,
    "auth_code_queue": queue.Queue(),
}


def set_web_status(status: str, task: str = "") -> None:
    _server_state["status"] = status
    if task:
        _server_state["task"] = task


def set_web_log_file(path: str) -> None:
    _server_state["log_file"] = path


def get_auth_code(timeout: float = 300.0) -> str | None:
    """Wait for an OAuth authorization code captured by the web server."""
    try:
        return _server_state["auth_code_queue"].get(timeout=timeout)
    except queue.Empty:
        return None


_HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TubeTape 控制台</title>
  <style>
    :root {
      --bg: #121214;
      --card: #1c1c1f;
      --border: #2e2e33;
      --text: #e1e1e6;
      --muted: #8b8b94;
      --accent: #e50914;
      --success: #10b981;
      --blue: #3b82f6;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: flex;
      flex-direction: column;
      height: 100vh;
      overflow: hidden;
    }
    header {
      background: var(--card);
      border-bottom: 1px solid var(--border);
      padding: 12px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-shrink: 0;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 700;
      font-size: 1.15rem;
    }
    .brand span {
      background: var(--accent);
      color: #fff;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 0.85rem;
      letter-spacing: 0.5px;
    }
    .status-badge {
      display: flex;
      align-items: center;
      gap: 8px;
      background: rgba(59, 130, 246, 0.15);
      border: 1px solid var(--blue);
      color: #93c5fd;
      padding: 4px 12px;
      border-radius: 999px;
      font-size: 0.85rem;
      font-weight: 500;
    }
    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #3b82f6;
      animation: pulse 2s infinite;
    }
    @keyframes pulse {
      0% { opacity: 0.4; }
      50% { opacity: 1; }
      100% { opacity: 0.4; }
    }
    .controls {
      display: flex;
      gap: 10px;
      align-items: center;
    }
    button {
      background: #2a2a30;
      color: var(--text);
      border: 1px solid var(--border);
      padding: 6px 14px;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.85rem;
      transition: background 0.15s;
    }
    button:hover { background: #383840; }
    button.active {
      background: rgba(16, 185, 129, 0.2);
      border-color: var(--success);
      color: #6ee7b7;
    }
    main {
      flex: 1;
      padding: 16px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }
    .log-box {
      flex: 1;
      background: #0d0d10;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.84rem;
      line-height: 1.55;
      color: #d4d4d8;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }
    .log-line-info { color: #93c5fd; }
    .log-line-warn { color: #fcd34d; font-weight: 600; }
    .log-line-error { color: #f87171; font-weight: 700; }
    .log-line-debug { color: #71717a; }
    footer {
      padding: 8px 20px;
      background: var(--card);
      border-top: 1px solid var(--border);
      font-size: 0.78rem;
      color: var(--muted);
      display: flex;
      justify-content: space-between;
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span>TubeTape</span>
      <div>实时运行控制台</div>
    </div>
    <div class="status-badge">
      <div class="status-dot"></div>
      <div id="status-text">初始化中...</div>
    </div>
    <div class="controls">
      <button id="autoscroll-btn" class="active" onclick="toggleAutoScroll()">自动滚动: 开</button>
      <button onclick="clearConsole()">清屏</button>
    </div>
  </header>
  <main>
    <div id="log-box" class="log-box">正在连接日志流...\\n</div>
  </main>
  <footer>
    <div id="footer-task">当前任务: -</div>
    <div id="footer-stats">已连接</div>
  </footer>

  <script>
    let autoScroll = true;
    let offset = 0;
    let isInitial = true;
    const logBox = document.getElementById('log-box');
    const statusText = document.getElementById('status-text');
    const footerTask = document.getElementById('footer-task');

    function toggleAutoScroll() {
      autoScroll = !autoScroll;
      const btn = document.getElementById('autoscroll-btn');
      btn.textContent = '自动滚动: ' + (autoScroll ? '开' : '关');
      btn.className = autoScroll ? 'active' : '';
    }

    function clearConsole() {
      logBox.textContent = '';
    }

    function formatLine(line) {
      if (line.includes(' ERROR ')) return `<span class="log-line-error">${escapeHtml(line)}</span>`;
      if (line.includes(' WARNING ')) return `<span class="log-line-warn">${escapeHtml(line)}</span>`;
      if (line.includes(' INFO ')) return `<span class="log-line-info">${escapeHtml(line)}</span>`;
      if (line.includes(' DEBUG ')) return `<span class="log-line-debug">${escapeHtml(line)}</span>`;
      return escapeHtml(line);
    }

    function escapeHtml(text) {
      return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    async function pollLogs() {
      try {
        const url = `/api/logs?offset=${offset}&initial=${isInitial ? 1 : 0}`;
        const res = await fetch(url);
        if (res.ok) {
          const data = await res.json();
          offset = data.offset;
          if (isInitial) {
            logBox.innerHTML = '';
            isInitial = false;
          }
          if (data.content) {
            const lines = data.content.split('\\n');
            for (let i = 0; i < lines.length; i++) {
              if (lines[i] || i < lines.length - 1) {
                logBox.innerHTML += formatLine(lines[i]) + '\\n';
              }
            }
            if (autoScroll) {
              logBox.scrollTop = logBox.scrollHeight;
            }
          }
        }
      } catch (e) {
        // network issue, retry
      }

      try {
        const sRes = await fetch('/api/status');
        if (sRes.ok) {
          const sData = await sRes.json();
          statusText.textContent = sData.status || 'running';
          footerTask.textContent = '当前任务: ' + (sData.task || sData.status || '-');
        }
      } catch (e) {}

      setTimeout(pollLogs, 1500);
    }

    pollLogs();
  </script>
</body>
</html>
"""

_AUTH_SUCCESS_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>TubeTape 授权成功</title>
  <style>
    body { background: #121214; color: #e1e1e6; font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .card { background: #1c1c1f; border: 1px solid #2e2e33; padding: 36px 48px; border-radius: 12px; text-align: center; max-width: 480px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
    h1 { color: #10b981; font-size: 1.6rem; margin-bottom: 12px; }
    p { color: #a1a1aa; line-height: 1.6; margin-bottom: 24px; }
    a { background: #3b82f6; color: #fff; text-decoration: none; padding: 8px 18px; border-radius: 6px; font-weight: 500; }
  </style>
</head>
<body>
  <div class="card">
    <h1>✅ Google OAuth 授权成功！</h1>
    <p>TubeTape 已接收到您的授权凭据，token.json 已自动保存。程序正在后台继续运行，您可以关闭此标签页。</p>
    <a href="/">返回实时控制台</a>
  </div>
</body>
</html>
"""


class _RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress noisy HTTP access log lines to keep application log clean
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Check for OAuth callback landing on the root or callback path
        if "code" in query:
            code = query["code"][0]
            _server_state["auth_code_queue"].put(code)
            _logger.info("web server intercepted OAuth authorization code")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_AUTH_SUCCESS_HTML.encode("utf-8"))
            return

        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_HTML_PAGE.encode("utf-8"))
            return

        if path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            payload = {
                "status": _server_state["status"],
                "task": _server_state["task"],
            }
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        if path == "/api/logs":
            log_file = _server_state.get("log_file")
            offset = 0
            try:
                offset = int(query.get("offset", [0])[0])
            except (ValueError, TypeError):
                offset = 0
            is_initial = query.get("initial", ["0"])[0] == "1"

            content = ""
            new_offset = offset

            if log_file and os.path.exists(log_file):
                try:
                    file_size = os.path.getsize(log_file)
                    # For initial load, limit to last 64 KB so browser doesn't choke
                    if is_initial and offset == 0 and file_size > 64 * 1024:
                        offset = max(0, file_size - 64 * 1024)

                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(offset)
                        content = f.read()
                        new_offset = f.tell()
                except OSError as exc:
                    content = f"[无法读取日志文件: {exc}]\\n"

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            payload = {"offset": new_offset, "content": content}
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return

        self.send_response(404)
        self.end_headers()


class WebServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8080, log_file: str | None = None):
        self.host = host
        self.port = port
        if log_file:
            set_web_log_file(log_file)
        self._server = None
        self._thread = None

    def start(self) -> None:
        try:
            self._server = ThreadingHTTPServer((self.host, self.port), _RequestHandler)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            _logger.info("web console started at http://%s:%d", self.host, self.port)
        except OSError as exc:
            _logger.warning("could not start web console on port %d: %s", self.port, exc)
            self._server = None

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            _logger.debug("web console stopped")
