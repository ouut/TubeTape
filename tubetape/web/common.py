"""Common styles, navigation bar, and helpers for TubeTape Web UI."""

from __future__ import annotations

COMMON_CSS = """
  :root {
    --bg-dark: #0b0f19;
    --bg-card: rgba(17, 24, 39, 0.85);
    --bg-card-hover: rgba(31, 41, 55, 0.95);
    --border: rgba(255, 255, 255, 0.08);
    --border-accent: rgba(59, 130, 246, 0.3);
    --primary: #3b82f6;
    --primary-hover: #2563eb;
    --emerald: #10b981;
    --amber: #f59e0b;
    --rose: #f43f5e;
    --indigo: #6366f1;
    --purple: #8b5cf6;
    --text-main: #f3f4f6;
    --text-muted: #9ca3af;
    --text-sub: #6b7280;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg-dark);
    color: var(--text-main);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    overflow-x: hidden;
  }

  /* Universal Navigation Bar */
  .navbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: rgba(15, 23, 42, 0.9);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border);
    padding: 0 24px;
    height: 60px;
    position: sticky;
    top: 0;
    z-index: 1000;
  }

  .nav-left {
    display: flex;
    align-items: center;
    gap: 28px;
  }

  .nav-brand {
    display: flex;
    align-items: center;
    gap: 10px;
    text-decoration: none;
    color: var(--text-main);
    font-weight: 700;
    font-size: 1.15rem;
    letter-spacing: -0.02em;
  }
  .nav-brand-logo {
    font-size: 1.35rem;
  }
  .nav-brand-tag {
    font-size: 0.7rem;
    background: rgba(59, 130, 246, 0.2);
    color: #60a5fa;
    padding: 2px 6px;
    border-radius: 4px;
    border: 1px solid rgba(59, 130, 246, 0.3);
    font-weight: 600;
  }

  .nav-links {
    display: flex;
    align-items: center;
    gap: 6px;
    list-style: none;
  }
  .nav-item a {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    text-decoration: none;
    color: var(--text-muted);
    font-size: 0.9rem;
    font-weight: 500;
    padding: 7px 14px;
    border-radius: 6px;
    transition: all 0.15s ease;
  }
  .nav-item a:hover {
    color: var(--text-main);
    background: rgba(255, 255, 255, 0.05);
  }
  .nav-item.active a {
    color: #fff;
    background: rgba(59, 130, 246, 0.2);
    border: 1px solid rgba(59, 130, 246, 0.35);
    font-weight: 600;
  }

  .nav-right {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .status-pill {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 5px 12px;
    border-radius: 20px;
    font-size: 0.8rem;
    font-weight: 500;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid var(--border);
  }
  .status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #6b7280;
  }
  .status-dot.idle { background: #10b981; }
  .status-dot.scanning { background: #3b82f6; animation: pulse 1.5s infinite; }
  .status-dot.transcoding { background: #f59e0b; animation: pulse 1.5s infinite; }
  .status-dot.uploading { background: #8b5cf6; animation: pulse 1.5s infinite; }
  .status-dot.syncing { background: #06b6d4; animation: pulse 1.5s infinite; }
  .status-dot.waiting_auth { background: #ec4899; animation: pulse 1.2s infinite; }
  .status-dot.error { background: #ef4444; }

  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.85); }
  }

  /* Global Buttons */
  .btn {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 7px 14px;
    font-size: 0.85rem;
    font-weight: 500;
    border-radius: 6px;
    border: 1px solid transparent;
    cursor: pointer;
    transition: all 0.15s ease;
    text-decoration: none;
    color: var(--text-main);
    background: rgba(255, 255, 255, 0.08);
  }
  .btn:hover { background: rgba(255, 255, 255, 0.14); }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }

  .btn-primary {
    background: var(--primary);
    color: #fff;
  }
  .btn-primary:hover { background: var(--primary-hover); }

  .btn-emerald {
    background: rgba(16, 185, 129, 0.15);
    border: 1px solid rgba(16, 185, 129, 0.4);
    color: #34d399;
  }
  .btn-emerald:hover { background: rgba(16, 185, 129, 0.28); }

  .btn-sync {
    background: rgba(59, 130, 246, 0.15);
    border: 1px solid rgba(59, 130, 246, 0.4);
    color: #60a5fa;
  }
  .btn-sync:hover { background: rgba(59, 130, 246, 0.28); }

  .btn-amber {
    background: rgba(245, 158, 11, 0.15);
    border: 1px solid rgba(245, 158, 11, 0.4);
    color: #fbbf24;
  }
  .btn-amber:hover { background: rgba(245, 158, 11, 0.28); }

  .btn-danger {
    background: rgba(239, 68, 68, 0.15);
    border: 1px solid rgba(239, 68, 68, 0.4);
    color: #f87171;
  }
  .btn-danger:hover { background: rgba(239, 68, 68, 0.28); }

  .btn-sm {
    padding: 4px 10px;
    font-size: 0.78rem;
    border-radius: 4px;
  }

  /* Cards and Layout Containers */
  .container {
    width: 100%;
    max-width: 1360px;
    margin: 0 auto;
    padding: 24px;
    flex: 1;
  }

  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    margin-bottom: 20px;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.25);
  }
  .card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.05);
    padding-bottom: 12px;
  }
  .card-title {
    font-size: 1.05rem;
    font-weight: 600;
    color: var(--text-main);
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .card-subtitle {
    font-size: 0.8rem;
    color: var(--text-muted);
  }

  /* Badges */
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 8px;
    border-radius: 4px;
    font-size: 0.75rem;
    font-weight: 600;
    text-transform: uppercase;
  }
  .badge-sealed { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
  .badge-pending { background: rgba(245, 158, 11, 0.2); color: #fbbf24; border: 1px solid rgba(245, 158, 11, 0.3); }
  .badge-failed { background: rgba(239, 68, 68, 0.2); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }
  .badge-info { background: rgba(59, 130, 246, 0.2); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.3); }

  /* Progress bars */
  .prog-track {
    width: 100%;
    height: 8px;
    background: rgba(255, 255, 255, 0.08);
    border-radius: 4px;
    overflow: hidden;
    position: relative;
  }
  .prog-bar {
    height: 100%;
    background: linear-gradient(90deg, #3b82f6, #60a5fa);
    border-radius: 4px;
    transition: width 0.3s ease;
  }
  .prog-bar.emerald { background: linear-gradient(90deg, #10b981, #34d399); }
  .prog-bar.purple { background: linear-gradient(90deg, #8b5cf6, #c084fc); }

  /* Notification Toast */
  #toast {
    position: fixed;
    bottom: 24px;
    right: 24px;
    background: rgba(17, 24, 39, 0.95);
    color: #fff;
    padding: 12px 20px;
    border-radius: 8px;
    border: 1px solid rgba(255, 255, 255, 0.15);
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
    z-index: 9999;
    display: none;
    font-size: 0.9rem;
    backdrop-filter: blur(8px);
  }
"""


def render_navbar(active_page: str = "dashboard") -> str:
    """Render universal top navigation bar."""
    pages = [
        ("dashboard", "/", "📊 控制台"),
        ("view", "/view", "🖼️ 视界画廊"),
        ("segments", "/segments", "🎞️ 分片管理"),
        ("logs", "/logs", "📜 运行日志"),
        ("settings", "/settings", "⚙️ 系统配置"),
    ]

    links_html = []
    for pid, url, title in pages:
        active_cls = " active" if pid == active_page else ""
        links_html.append(f'<li class="nav-item{active_cls}"><a href="{url}">{title}</a></li>')

    links_str = "".join(links_html)
    tpl = """
  <nav class="navbar">
    <div class="nav-left">
      <a href="/" class="nav-brand">
        <span class="nav-brand-logo">📼</span>
        <span>TubeTape</span>
        <span class="nav-brand-tag">v1.6.2</span>
      </a>
      <ul class="nav-links">
        __NAV_LINKS__
      </ul>
    </div>
    <div class="nav-right">
      <div class="status-pill" id="global-status-pill">
        <span class="status-dot idle" id="global-status-dot"></span>
        <span id="global-status-text">空闲</span>
      </div>
      <button class="btn btn-sync btn-sm" id="nav-sync-btn" onclick="triggerSyncYoutube()" title="从 YouTube 频道拉取最新视频列表并双向对齐">☁️ 同步</button>
      <button class="btn btn-emerald btn-sm" id="nav-scan-btn" onclick="triggerFullScan()" title="执行全量媒体文件扫描">🔄 扫描</button>
    </div>
  </nav>
  <div id="toast"></div>
  <script>
    function showToast(msg, isError = false) {
      const t = document.getElementById('toast');
      if (!t) return;
      t.textContent = msg;
      t.style.borderColor = isError ? '#f87171' : 'rgba(255, 255, 255, 0.2)';
      t.style.display = 'block';
      setTimeout(() => { t.style.display = 'none'; }, 3500);
    }

    async function triggerFullScan() {
      if (!confirm('确定要开始全量扫描吗？')) return;
      const btn = document.getElementById('nav-scan-btn');
      if (btn) btn.disabled = true;
      try {
        const res = await fetch('/api/scan/start', { method: 'POST' });
        const d = await res.json();
        if (res.ok && d.ok) {
          showToast('✅ ' + (d.message || '全量扫描已在后台启动'));
        } else {
          showToast('❌ 启动失败: ' + (d.error || '未知错误'), true);
        }
      } catch (err) {
        showToast('❌ 请求失败: ' + err.message, true);
      } finally {
        if (btn) btn.disabled = false;
      }
    }

    async function triggerSyncYoutube() {
      const btn = document.getElementById('nav-sync-btn');
      if (btn) {
        btn.disabled = true;
        btn.textContent = '⏳ 同步中...';
      }
      try {
        const res = await fetch('/api/sync_youtube', { method: 'POST' });
        const d = await res.json();
        if (res.ok && d.ok) {
          showToast('✅ ' + (d.message || 'YouTube 同步完成'));
          if (typeof pollDashboard === 'function') pollDashboard();
          if (typeof loadSegments === 'function') loadSegments();
        } else {
          showToast('❌ 同步失败: ' + (d.error || '未知错误'), true);
        }
      } catch (err) {
        showToast('❌ 请求失败: ' + err.message, true);
      } finally {
        if (btn) {
          btn.disabled = false;
          btn.textContent = '☁️ 同步';
        }
      }
    }

    async function pollNavStatus() {
      try {
        const res = await fetch('/api/status');
        if (!res.ok) return;
        const d = await res.json();
        const dot = document.getElementById('global-status-dot');
        const txt = document.getElementById('global-status-text');
        if (!dot || !txt) return;
        const st = d.status || 'idle';
        dot.className = 'status-dot ' + st;
        const statusMap = {
          idle: '空闲',
          scanning: '扫描中',
          transcoding: '转码中',
          uploading: '上传中',
          syncing: '云端同步',
          waiting_auth: '等待授权',
          rebuilding: '重建中',
          error: '错误'
        };
        txt.textContent = statusMap[st] || st;
      } catch (e) {}
    }
    setInterval(pollNavStatus, 2500);
    pollNavStatus();
  </script>
"""
    return tpl.replace("__NAV_LINKS__", links_str)
