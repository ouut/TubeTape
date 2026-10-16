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

    .btn:disabled {
      opacity: 0.4;
      cursor: not-allowed;
      pointer-events: none;
    }

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
      <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap;">
        <div style="display: flex; align-items: center; gap: 5px;">
          <select id="page-size-select" onchange="changePageSize(this.value)" style="padding: 4px 8px; font-size: 0.8rem; background: rgba(255,255,255,0.06); border: 1px solid var(--border); color: #fff; border-radius: 6px; cursor: pointer;">
            <option value="50">50 行/页</option>
            <option value="100" selected>100 行/页</option>
            <option value="200">200 行/页</option>
            <option value="500">500 行/页</option>
            <option value="1000">1000 行/页</option>
          </select>
          <button class="btn btn-sm" id="btn-first" onclick="goToPage(1)" title="首页">⏮️</button>
          <button class="btn btn-sm" id="btn-prev" onclick="goToPage(currentPage - 1)" title="上一页">◀</button>
          <span id="page-info" style="font-size: 0.8rem; color: var(--text-muted); min-width: 110px; text-align: center;">1 / 1</span>
          <button class="btn btn-sm" id="btn-next" onclick="goToPage(currentPage + 1)" title="下一页">▶</button>
          <button class="btn btn-sm" id="btn-last" onclick="goToPage(totalPages)" title="末页">⏭️</button>
        </div>
        <label class="autoscroll-toggle">
          <input type="checkbox" id="auto-scroll" checked onchange="toggleAutoScroll(this.checked)">
          <span>跟随最新</span>
        </label>
        <button class="btn btn-sm" onclick="clearLogs()">清空</button>
        <a href="/api/logs?download=1" class="btn btn-sm btn-primary">⬇️ 下载日志</a>
      </div>
    </div>

    <div class="log-box" id="log-box"></div>
  </div>

  <script>
    let logOffset = 0;
    let logLines = [];
    let currentLevel = 'ALL';
    let isPolling = false;
    let pageSize = 100;
    let currentPage = 1;
    let totalPages = 1;

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
          let added = false;
          for (const line of newLines) {
            if (line.trim().length > 0) {
              logLines.push(line);
              added = true;
            }
          }
          if (added) {
            renderLogLines();
          }
        }
      } catch (e) {} finally {
        isPolling = false;
      }
    }

    function toggleAutoScroll(enabled) {
      if (enabled) {
        currentPage = totalPages;
        renderLogLines();
      }
    }

    function changePageSize(val) {
      pageSize = parseInt(val, 10) || 100;
      currentPage = 1;
      renderLogLines();
    }

    function goToPage(page) {
      const autoScrollCheckbox = document.getElementById('auto-scroll');
      if (page < 1) page = 1;
      if (page > totalPages) page = totalPages;
      if (page !== totalPages && autoScrollCheckbox.checked) {
        autoScrollCheckbox.checked = false;
      }
      currentPage = page;
      renderLogLines();
    }

    function setLogLevel(lvl, btn) {
      currentLevel = lvl;
      document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
      if (btn) btn.classList.add('active');
      currentPage = 1;
      renderLogLines();
    }

    function filterLogs() {
      currentPage = 1;
      renderLogLines();
    }

    function clearLogs() {
      logLines = [];
      currentPage = 1;
      totalPages = 1;
      renderLogLines();
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

      const totalLines = filtered.length;
      totalPages = Math.max(1, Math.ceil(totalLines / pageSize));

      if (autoScroll) {
        currentPage = totalPages;
      } else {
        if (currentPage > totalPages) currentPage = totalPages;
        if (currentPage < 1) currentPage = 1;
      }

      const startIdx = totalLines === 0 ? 0 : (currentPage - 1) * pageSize;
      const endIdx = Math.min(startIdx + pageSize, totalLines);
      const pageLines = filtered.slice(startIdx, endIdx);

      const pageInfo = document.getElementById('page-info');
      if (pageInfo) {
        pageInfo.textContent = `${currentPage} / ${totalPages} (${totalLines}条)`;
      }
      const btnFirst = document.getElementById('btn-first');
      const btnPrev = document.getElementById('btn-prev');
      const btnNext = document.getElementById('btn-next');
      const btnLast = document.getElementById('btn-last');
      if (btnFirst) btnFirst.disabled = (currentPage <= 1);
      if (btnPrev) btnPrev.disabled = (currentPage <= 1);
      if (btnNext) btnNext.disabled = (currentPage >= totalPages);
      if (btnLast) btnLast.disabled = (currentPage >= totalPages);

      if (pageLines.length === 0) {
        box.innerHTML = '<div style="color: var(--text-muted); text-align: center; margin-top: 40px;">暂无匹配日志记录</div>';
        return;
      }

      box.innerHTML = pageLines.map((line, idx) => {
        let cls = 'info';
        if (line.includes('ERROR') || line.includes('error')) cls = 'error';
        else if (line.includes('WARN') || line.includes('warn')) cls = 'warn';
        else if (line.includes('DEBUG') || line.includes('debug')) cls = 'debug';
        const escaped = line.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        const lineNum = startIdx + idx + 1;
        return `<div class="log-line ${cls}"><span style="color:#4b5563; user-select:none; display:inline-block; width:45px; text-align:right; margin-right:12px;">${lineNum}</span>${escaped}</div>`;
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
