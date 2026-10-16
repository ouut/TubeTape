"""Logs viewer page for TubeTape Web UI."""

from __future__ import annotations

from .common import COMMON_CSS, render_navbar

_LOGS_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>运行日志 · TubeTape</title>
  <style>
    __COMMON_CSS__

    .log-toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 12px;
      margin-bottom: 16px;
    }
    .log-controls-left {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .filter-btn-group {
      display: flex;
      border: 1px solid var(--border);
      border-radius: 6px;
      overflow: hidden;
    }
    .filter-btn {
      padding: 5px 12px;
      background: rgba(255, 255, 255, 0.04);
      border: none;
      color: var(--text-muted);
      font-size: 0.8rem;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .filter-btn:not(:last-child) { border-right: 1px solid var(--border); }
    .filter-btn.active {
      background: rgba(59, 130, 246, 0.25);
      color: #fff;
      font-weight: 600;
    }

    .log-search {
      padding: 6px 12px;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--border);
      border-radius: 6px;
      color: #fff;
      font-size: 0.82rem;
      width: 200px;
    }
    .log-search:focus { outline: none; border-color: var(--primary); }

    /* Monospace Terminal Box */
    .log-box {
      width: 100%;
      height: calc(100vh - 200px);
      min-height: 500px;
      background: #060911;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 16px;
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
      font-size: 0.82rem;
      line-height: 1.6;
      color: #d1d5db;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }
    .log-line {
      padding: 1px 0;
    }
    .log-line.info { color: #93c5fd; }
    .log-line.warn { color: #fbbf24; }
    .log-line.error { color: #f87171; font-weight: 600; }
    .log-line.debug { color: #6b7280; }

    .autoscroll-toggle {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font-size: 0.8rem;
      color: var(--text-muted);
      cursor: pointer;
    }
  </style>
</head>
<body>
  __NAVBAR__

  <div class="container">
    <div class="log-toolbar">
      <div class="log-controls-left">
        <h2 style="font-size: 1.25rem; font-weight: 700;">📜 实时运行日志</h2>
        <div class="filter-btn-group">
          <button class="filter-btn active" onclick="setLogLevel('ALL', this)">全部</button>
          <button class="filter-btn" onclick="setLogLevel('INFO', this)">INFO</button>
          <button class="filter-btn" onclick="setLogLevel('WARNING', this)">WARN</button>
          <button class="filter-btn" onclick="setLogLevel('ERROR', this)">ERROR</button>
          <button class="filter-btn" onclick="setLogLevel('DEBUG', this)">DEBUG</button>
        </div>
        <input type="text" id="log-search-input" class="log-search" placeholder="过滤关键字..." oninput="filterLogs()">
      </div>
      <div style="display: flex; align-items: center; gap: 14px;">
        <label class="autoscroll-toggle">
          <input type="checkbox" id="auto-scroll" checked>
          <span>自动滚屏</span>
        </label>
        <button class="btn btn-sm" onclick="clearLogs()">清空视图</button>
        <a href="/api/logs?download=1" class="btn btn-sm btn-primary">⬇️ 下载完整日志</a>
      </div>
    </div>

    <div class="log-box" id="log-box"></div>
  </div>

  <script>
    let logOffset = 0;
    let logLines = [];
    let currentLevel = 'ALL';
    let isPolling = false;

    async function pollLogs() {
      if (isPolling) return;
      isPolling = true;
      try {
        const res = await fetch(`/api/logs?offset=${logOffset}`);
        if (!res.ok) return;
        const data = await res.json();
        if (data.content && data.content.length > 0) {
          logOffset = data.offset;
          const newLines = data.content.split('\\n');
          for (const line of newLines) {
            if (line.trim().length > 0) {
              logLines.push(line);
            }
          }
          renderLogLines();
        }
      } catch (e) {} finally {
        isPolling = false;
      }
    }

    function setLogLevel(lvl, btn) {
      currentLevel = lvl;
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      if (btn) btn.classList.add('active');
      renderLogLines();
    }

    function filterLogs() {
      renderLogLines();
    }

    function clearLogs() {
      logLines = [];
      document.getElementById('log-box').innerHTML = '';
    }

    function renderLogLines() {
      const q = (document.getElementById('log-search-input').value || '').trim().toLowerCase();
      const box = document.getElementById('log-box');
      const autoScroll = document.getElementById('auto-scroll').checked;

      const filtered = logLines.filter(line => {
        if (currentLevel !== 'ALL') {
          if (currentLevel === 'INFO' && !line.includes('INFO')) return false;
          if (currentLevel === 'WARNING' && !line.includes('WARN')) return false;
          if (currentLevel === 'ERROR' && !line.includes('ERROR')) return false;
          if (currentLevel === 'DEBUG' && !line.includes('DEBUG')) return false;
        }
        if (q && !line.toLowerCase().includes(q)) return false;
        return true;
      });

      box.innerHTML = filtered.map(line => {
        let cls = 'info';
        if (line.includes('ERROR') || line.includes('error')) cls = 'error';
        else if (line.includes('WARN') || line.includes('warn')) cls = 'warn';
        else if (line.includes('DEBUG') || line.includes('debug')) cls = 'debug';
        const escaped = line.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        return `<div class="log-line ${cls}">${escaped}</div>`;
      }).join('');

      if (autoScroll) {
        box.scrollTop = box.scrollHeight;
      }
    }

    pollLogs();
    setInterval(pollLogs, 1500);
  </script>
</body>
</html>"""


def render_logs_html() -> str:
    """Render full HTML for the Logs viewer page."""
    navbar = render_navbar(active_page="logs")
    return _LOGS_HTML_TEMPLATE.replace("__COMMON_CSS__", COMMON_CSS).replace("__NAVBAR__", navbar)
