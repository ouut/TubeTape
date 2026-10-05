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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_logger = logging.getLogger("tubetape.web")

# Shared state updated by Reporter or CLI
_server_state = {
    "status": "idle",
    "task": "",
    "log_file": None,
    "auth_code_queue": queue.Queue(),
    "oauth_session": None,
    "auth_event": threading.Event(),
    "auth_error": None,
    "is_running": False,
    "host": "0.0.0.0",
    "port": 8080,
}


def is_running() -> bool:
    """Return True if WebServer is currently running."""
    return _server_state["is_running"]


def get_web_port() -> int:
    """Return the actual bound port of the WebServer."""
    return _server_state.get("port", 8080)


def set_web_status(status: str, task: str = "") -> None:
    _server_state["status"] = status
    if task:
        _server_state["task"] = task


def set_web_log_file(path: str) -> None:
    _server_state["log_file"] = path


def set_web_oauth_session(session) -> None:
    """Register an active OAuthSession waiting for authorization."""
    _server_state["oauth_session"] = session
    _server_state["auth_event"].clear()
    _server_state["auth_error"] = None
    _server_state["status"] = "waiting_auth"
    _server_state["task"] = "等待 Google OAuth 授权（请访问 Web 控制台）"


def get_web_oauth_session():
    return _server_state.get("oauth_session")


def clear_web_oauth_session() -> None:
    _server_state["oauth_session"] = None
    if _server_state["status"] == "waiting_auth":
        _server_state["status"] = "idle"
        _server_state["task"] = ""


def wait_for_auth(timeout: float = 3600.0):
    """Wait for OAuth authorization via web callback or URL submission.

    Loops in 1-second intervals so KeyboardInterrupt/SIGTERM can be caught immediately.
    """
    start = time.monotonic()
    event = _server_state["auth_event"]

    while not event.is_set():
        if time.monotonic() - start > timeout:
            raise TimeoutError("等待 Google OAuth 授权超时（超过 1 小时）")
        event.wait(timeout=1.0)

    if _server_state["auth_error"]:
        err = _server_state["auth_error"]
        _server_state["auth_error"] = None
        raise RuntimeError(f"Google OAuth 授权失败: {err}")

    session = _server_state.get("oauth_session")
    if session and session.credentials:
        creds = session.credentials
        clear_web_oauth_session()
        return creds

    raise RuntimeError("OAuth 授权已完成但未找到有效凭据")


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
    .auth-banner {
      background: #181b24;
      border: 1px solid #3b82f6;
      border-radius: 8px;
      padding: 16px 20px;
      margin-bottom: 14px;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
      flex-shrink: 0;
    }
    .auth-banner-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 8px;
    }
    .auth-banner-title {
      font-size: 1.05rem;
      color: #93c5fd;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .auth-icon { font-size: 1.2rem; }
    .auth-badge {
      background: rgba(245, 158, 11, 0.2);
      border: 1px solid #f59e0b;
      color: #fbbf24;
      padding: 2px 10px;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .auth-banner-desc {
      font-size: 0.88rem;
      color: #a1a1aa;
      line-height: 1.5;
      margin-bottom: 14px;
    }
    .auth-methods {
      display: grid;
      grid-template-columns: 1fr 1.2fr;
      gap: 14px;
    }
    @media (max-width: 768px) {
      .auth-methods { grid-template-columns: 1fr; }
    }
    .auth-method-card {
      background: #121316;
      border: 1px solid #2e2e33;
      border-radius: 6px;
      padding: 12px 14px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
    }
    .auth-method-title {
      font-size: 0.88rem;
      font-weight: 600;
      color: #e1e1e6;
      margin-bottom: 6px;
    }
    .auth-method-desc {
      font-size: 0.78rem;
      color: #8b8b94;
      line-height: 1.45;
      margin-bottom: 10px;
    }
    .auth-btn-primary {
      display: inline-block;
      text-align: center;
      background: #2563eb;
      color: #fff;
      text-decoration: none;
      padding: 8px 14px;
      border-radius: 6px;
      font-size: 0.85rem;
      font-weight: 600;
      transition: background 0.15s;
    }
    .auth-btn-primary:hover { background: #1d4ed8; }
    .auth-input-row {
      display: flex;
      gap: 8px;
      margin-bottom: 6px;
    }
    .auth-input-row input {
      flex: 1;
      background: #0d0d10;
      border: 1px solid #383840;
      border-radius: 6px;
      color: #e1e1e6;
      padding: 6px 10px;
      font-size: 0.82rem;
      outline: none;
    }
    .auth-input-row input:focus {
      border-color: #3b82f6;
    }
    .auth-btn-submit {
      background: #10b981;
      border-color: #10b981;
      color: #fff;
      font-weight: 600;
      white-space: nowrap;
    }
    .auth-btn-submit:hover { background: #059669; }
    .auth-feedback {
      font-size: 0.8rem;
      min-height: 1.2rem;
    }
    .fb-error { color: #f87171; font-weight: 600; }
    .fb-success { color: #34d399; font-weight: 600; }
    .fb-info { color: #93c5fd; }
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
    <div id="auth-banner" class="auth-banner" style="display: none;">
      <div class="auth-banner-header">
        <div class="auth-banner-title">
          <span class="auth-icon">🔑</span>
          <strong>需要完成 Google YouTube 授权</strong>
        </div>
        <div class="auth-badge">等待授权中</div>
      </div>
      <div class="auth-banner-desc">
        TubeTape 尚未获得 YouTube 访问凭据（<code>token.json</code> 不存在或已失效）。请通过以下任一方式完成授权以开始同步：
      </div>
      <div class="auth-methods">
        <div class="auth-method-card">
          <div>
            <div class="auth-method-title">方式一：一键直接授权（本机或端口映射环境）</div>
            <div class="auth-method-desc">点击下方按钮前往 Google 登录授权。若本机可直接访问 <code>http://localhost:8080</code>，授权后将自动回调完成凭据保存并继续运行。</div>
          </div>
          <a id="auth-link-btn" href="#" target="_blank" class="auth-btn-primary">🔗 点击前往 Google 账号授权</a>
        </div>
        <div class="auth-method-card">
          <div>
            <div class="auth-method-title">方式二：手动粘贴地址栏 URL（远程 NAS / 无桌面服务器）</div>
            <div class="auth-method-desc">若在局域网 NAS 上运行，点击上方授权后，浏览器跳转 <code>http://localhost:8080</code> 可能会提示“无法访问此网站”。<strong>不必担心</strong>，直接将浏览器地址栏中的完整 URL 复制并粘贴到下方即可：</div>
          </div>
          <div class="auth-input-row">
            <input type="text" id="auth-url-input" placeholder="粘贴浏览器地址栏完整 URL（形如 http://localhost:8080/?state=...&code=...）或 code..." />
            <button id="auth-submit-btn" class="btn auth-btn-submit" onclick="submitAuthCode()">提交授权凭据</button>
          </div>
          <div id="auth-feedback" class="auth-feedback"></div>
        </div>
      </div>
    </div>
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
    let currentAuthUrl = '';
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

    async function submitAuthCode() {
      const input = document.getElementById('auth-url-input');
      const btn = document.getElementById('auth-submit-btn');
      const fb = document.getElementById('auth-feedback');
      const val = input.value.trim();
      if (!val) {
        fb.innerHTML = '<span class="fb-error">请先粘贴 URL 或授权码！</span>';
        return;
      }

      btn.disabled = true;
      btn.textContent = '正在提交...';
      fb.innerHTML = '<span class="fb-info">正在向 Google 换取 Token...</span>';

      try {
        const res = await fetch('/api/auth/submit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: val })
        });
        const data = await res.json();
        if (res.ok && data.ok) {
          fb.innerHTML = '<span class="fb-success">✅ 授权成功！token.json 已保存，后台已自动继续运行。</span>';
          input.value = '';
          setTimeout(() => {
            document.getElementById('auth-banner').style.display = 'none';
          }, 2500);
        } else {
          fb.innerHTML = `<span class="fb-error">❌ 换取失败: ${escapeHtml(data.error || '未知错误')}</span>`;
        }
      } catch (err) {
        fb.innerHTML = `<span class="fb-error">❌ 网络请求失败: ${escapeHtml(err.message)}</span>`;
      } finally {
        btn.disabled = false;
        btn.textContent = '提交授权凭据';
      }
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

          const authBanner = document.getElementById('auth-banner');
          if (sData.auth_required && sData.auth_url) {
            authBanner.style.display = 'block';
            if (currentAuthUrl !== sData.auth_url) {
              currentAuthUrl = sData.auth_url;
              document.getElementById('auth-link-btn').href = sData.auth_url;
            }
          } else {
            authBanner.style.display = 'none';
          }
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
    body { background: #121214; color: #e1e1e6; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .card { background: #1c1c1f; border: 1px solid #2e2e33; padding: 36px 48px; border-radius: 12px; text-align: center; max-width: 480px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
    h1 { color: #10b981; font-size: 1.5rem; margin-bottom: 12px; }
    p { color: #a1a1aa; line-height: 1.6; margin-bottom: 24px; font-size: 0.95rem; }
    a { display: inline-block; background: #3b82f6; color: #fff; text-decoration: none; padding: 10px 22px; border-radius: 6px; font-weight: 500; font-size: 0.9rem; transition: background 0.2s; }
    a:hover { background: #2563eb; }
    .hint { margin-top: 14px; font-size: 0.8rem; color: #71717a; }
  </style>
</head>
<body>
  <div class="card">
    <h1>✅ Google OAuth 授权成功！</h1>
    <p>TubeTape 已成功接收到您的 Google 授权凭据，token.json 已自动保存。后台正在自动继续处理媒体与上传，无需重启容器。</p>
    <a href="/">返回实时控制台</a>
    <div class="hint">3 秒后将自动跳转返回控制台...</div>
  </div>
  <script>
    setTimeout(function() { window.location.href = '/'; }, 3000);
  </script>
</body>
</html>
"""

_AUTH_ERROR_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>TubeTape 授权失败</title>
  <style>
    body { background: #121214; color: #e1e1e6; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .card { background: #1c1c1f; border: 1px solid #ef4444; padding: 36px 48px; border-radius: 12px; text-align: center; max-width: 480px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
    h1 { color: #ef4444; font-size: 1.5rem; margin-bottom: 12px; }
    p { color: #a1a1aa; line-height: 1.6; margin-bottom: 20px; font-size: 0.95rem; }
    .error-box { background: rgba(239, 68, 68, 0.1); border: 1px solid #7f1d1d; color: #fca5a5; padding: 10px; border-radius: 6px; font-family: monospace; font-size: 0.85rem; margin-bottom: 24px; word-break: break-all; }
    a { display: inline-block; background: #3b82f6; color: #fff; text-decoration: none; padding: 10px 22px; border-radius: 6px; font-weight: 500; font-size: 0.9rem; }
    a:hover { background: #2563eb; }
  </style>
</head>
<body>
  <div class="card">
    <h1>❌ Google OAuth 授权失败</h1>
    <p>凭据换取失败，错误信息如下：</p>
    <div class="error-box">{error}</div>
    <a href="/">返回控制台重试</a>
  </div>
</body>
</html>
"""


class _RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress noisy HTTP access log lines to keep application log clean
        pass

    def _send_json(self, status: int, data: dict) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _handle_auth_exchange(self, target: str) -> None:
        session = _server_state.get("oauth_session")
        if not session:
            self._send_json(400, {"ok": False, "error": "当前未在等待 OAuth 授权或授权已完成"})
            return

        if not target:
            self._send_json(400, {"ok": False, "error": "未提供授权 URL 或授权码"})
            return

        try:
            session.exchange(target)
            _server_state["auth_event"].set()
            _server_state["auth_code_queue"].put(target)
            _logger.info("OAuth authorization completed via manual submit")
            self._send_json(200, {"ok": True, "message": "授权成功，token.json 已保存"})
        except Exception as exc:
            _logger.warning("OAuth exchange failed: %s", exc)
            self._send_json(400, {"ok": False, "error": str(exc)})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/auth/submit":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""
            target = ""
            if body:
                try:
                    data = json.loads(body)
                    target = data.get("url") or data.get("code") or ""
                except json.JSONDecodeError:
                    target = body.strip()
            self._handle_auth_exchange(target)
            return

        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Check for OAuth callback landing on the root or callback path
        if "code" in query:
            code = query["code"][0]
            session = _server_state.get("oauth_session")
            if session:
                try:
                    session.exchange(code)
                    _server_state["auth_event"].set()
                    _server_state["auth_code_queue"].put(code)
                    _logger.info("web server intercepted and exchanged OAuth code")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(_AUTH_SUCCESS_HTML.encode("utf-8"))
                    return
                except Exception as exc:
                    _logger.error("OAuth exchange failed on callback: %s", exc)
                    _server_state["auth_error"] = str(exc)
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(_AUTH_ERROR_HTML.format(error=str(exc)).encode("utf-8"))
                    return
            else:
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
            session = _server_state.get("oauth_session")
            auth_required = session is not None and not _server_state["auth_event"].is_set()
            auth_url = session.auth_url if auth_required else None
            payload = {
                "status": _server_state["status"],
                "task": _server_state["task"],
                "auth_required": auth_required,
                "auth_url": auth_url,
            }
            self._send_json(200, payload)
            return

        if path == "/api/auth/submit":
            target = query.get("url", [None])[0] or query.get("code", [None])[0] or ""
            self._handle_auth_exchange(target)
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

            payload = {"offset": new_offset, "content": content}
            self._send_json(200, payload)
            return

        self.send_response(404)
        self.end_headers()


class WebServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 8080, log_file: str | None = None):
        self.host = host
        self.port = port
        _server_state["host"] = host
        _server_state["port"] = port
        if log_file:
            set_web_log_file(log_file)
        self._server = None
        self._thread = None

    def start(self) -> None:
        try:
            self._server = ThreadingHTTPServer((self.host, self.port), _RequestHandler)
            _server_state["port"] = self._server.server_port
            _server_state["is_running"] = True
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            _logger.info("web console started at http://%s:%d", self.host, self._server.server_port)
        except OSError as exc:
            _logger.warning("could not start web console on port %d: %s", self.port, exc)
            self._server = None
            _server_state["is_running"] = False

    def stop(self) -> None:
        _server_state["is_running"] = False
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            _logger.debug("web console stopped")
