"""Dashboard page for TubeTape Web UI."""

from __future__ import annotations

from .common import COMMON_CSS, render_navbar

_DASHBOARD_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>控制台 · TubeTape</title>
  <style>
    __COMMON_CSS__

    /* Metric Grid */
    .metric-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }
    .metric-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px 20px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      transition: transform 0.15s ease, border-color 0.15s ease;
    }
    .metric-card:hover {
      transform: translateY(-2px);
      border-color: var(--border-accent);
    }
    .metric-icon {
      font-size: 1.6rem;
      margin-bottom: 8px;
    }
    .metric-title {
      font-size: 0.8rem;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
      font-weight: 600;
    }
    .metric-value {
      font-size: 1.55rem;
      font-weight: 700;
      color: var(--text-main);
      margin: 4px 0 2px 0;
    }
    .metric-sub {
      font-size: 0.75rem;
      color: var(--text-sub);
    }

    /* Main Dashboard Layout */
    .dashboard-layout {
      display: grid;
      grid-template-columns: 2fr 1fr;
      gap: 24px;
    }
    @media (max-width: 960px) {
      .dashboard-layout { grid-template-columns: 1fr; }
    }

    /* Task Live Card */
    .task-indicator {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 14px 16px;
      background: rgba(255, 255, 255, 0.03);
      border-radius: 8px;
      border: 1px solid var(--border);
      margin-bottom: 16px;
    }
    .task-spinner {
      width: 22px;
      height: 22px;
      border: 3px solid rgba(59, 130, 246, 0.2);
      border-top-color: var(--primary);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    .task-desc {
      font-size: 0.95rem;
      font-weight: 600;
      color: var(--text-main);
    }
    .task-meta {
      font-size: 0.8rem;
      color: var(--text-muted);
      margin-top: 2px;
    }

    /* Section quick links */
    .quick-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 14px;
    }

    /* Auth Box */
    .auth-banner {
      padding: 14px;
      border-radius: 8px;
      background: rgba(236, 72, 153, 0.1);
      border: 1px solid rgba(236, 72, 153, 0.3);
      margin-bottom: 14px;
    }
    .auth-input {
      width: 100%;
      padding: 8px 12px;
      background: rgba(0, 0, 0, 0.3);
      border: 1px solid var(--border);
      border-radius: 6px;
      color: #fff;
      font-size: 0.85rem;
      margin-bottom: 8px;
    }
    .auth-input:focus { outline: none; border-color: var(--primary); }

    /* Upload Queue List */
    .queue-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 8px 12px;
      background: rgba(255, 255, 255, 0.02);
      border-radius: 6px;
      margin-bottom: 6px;
      font-size: 0.85rem;
    }
  </style>
</head>
<body>
  __NAVBAR__

  <div class="container">
    <!-- Top Metrics Overview -->
    <div class="metric-grid">
      <div class="metric-card">
        <span class="metric-icon">📁</span>
        <span class="metric-title">媒体文件总量</span>
        <div class="metric-value" id="stat-total-files">--</div>
        <div class="metric-sub" id="stat-total-size">计算中...</div>
      </div>
      <div class="metric-card">
        <span class="metric-icon">📸</span>
        <span class="metric-title">相册照片</span>
        <div class="metric-value" id="stat-photos">--</div>
        <div class="metric-sub">JPG / PNG / HEIC / WebP</div>
      </div>
      <div class="metric-card">
        <span class="metric-icon">🎥</span>
        <span class="metric-title">拍摄视频</span>
        <div class="metric-value" id="stat-videos">--</div>
        <div class="metric-sub">MP4 / MOV / 伴随视频</div>
      </div>
      <div class="metric-card">
        <span class="metric-icon">🎞️</span>
        <span class="metric-title">规划分段</span>
        <div class="metric-value" id="stat-segments">--</div>
        <div class="metric-sub" id="stat-segments-uploaded">已完成上传: --</div>
      </div>
      <div class="metric-card">
        <span class="metric-icon">⏱️</span>
        <span class="metric-title">时间跨度</span>
        <div class="metric-value" style="font-size: 1.1rem; padding-top: 6px;" id="stat-date-span">--</div>
        <div class="metric-sub" id="stat-skipped">过滤 Live Photos: --</div>
      </div>
    </div>

    <div class="dashboard-layout">
      <!-- Left Column: Current Task & Activity -->
      <div>
        <!-- Live Task Status Card -->
        <div class="card">
          <div class="card-header">
            <div class="card-title">
              <span>⚡ 当前后台任务</span>
            </div>
            <div class="card-subtitle" id="task-badge">实时更新</div>
          </div>

          <div id="task-content">
            <div class="task-indicator">
              <span style="font-size: 1.4rem;">☕</span>
              <div>
                <div class="task-desc">系统处于空闲待命状态</div>
                <div class="task-meta">暂无活跃的转码或上传任务，可点击下方操作手动触发</div>
              </div>
            </div>
          </div>

          <!-- Quick Actions -->
          <div style="margin-top: 18px; border-top: 1px solid rgba(255, 255, 255, 0.05); padding-top: 14px;">
            <div style="font-size: 0.8rem; color: var(--text-muted); margin-bottom: 8px; font-weight: 600;">快捷操作:</div>
            <div class="quick-actions">
              <button class="btn btn-emerald" onclick="triggerFullScan()">🔄 全量文件扫描</button>
              <button class="btn btn-sync" onclick="triggerSyncYoutube()">☁️ 与 YouTube 同步</button>
              <a href="/segments" class="btn">🎞️ 管理分片与排除</a>
              <a href="/view" class="btn btn-primary">🖼️ 打开沉浸画廊</a>
              <a href="/settings" class="btn">⚙️ 调整配置参数</a>
              <a href="/logs" class="btn">📜 查看详细日志</a>
            </div>
          </div>
        </div>

        <!-- Upload Queue Card -->
        <div class="card">
          <div class="card-header">
            <div class="card-title">
              <span>☁️ YouTube 上传队列</span>
            </div>
            <span class="badge badge-info" id="queue-count-badge">0 个排队中</span>
          </div>
          <div id="upload-queue-container">
            <div style="color: var(--text-muted); font-size: 0.85rem; text-align: center; padding: 20px 0;">
              暂无待上传或排队中的分片视频
            </div>
          </div>
        </div>
      </div>

      <!-- Right Column: System & Auth Info -->
      <div>
        <!-- Google OAuth Authentication Card -->
        <div class="card">
          <div class="card-header">
            <div class="card-title">
              <span>🔐 Google 授权状态</span>
            </div>
            <div id="auth-status-badge"><span class="badge badge-sealed">已就绪</span></div>
          </div>

          <div id="auth-box-content">
            <div style="font-size: 0.85rem; color: var(--text-muted); line-height: 1.5; margin-bottom: 12px;">
              YouTube 上传服务依赖 Google OAuth 凭据。
            </div>
            <div id="auth-details" style="display: none;">
              <div class="auth-banner">
                <div style="font-weight: 600; color: #f43f5e; margin-bottom: 6px;">⚠️ 尚未完成 YouTube 授权</div>
                <div style="font-size: 0.8rem; color: #fda4af; margin-bottom: 10px;">
                  请点击下方按钮打开 Google 授权窗口，完成授权后将回调 URL 粘贴到输入框中。
                </div>
                <a href="#" id="auth-link-btn" target="_blank" class="btn btn-primary btn-sm" style="margin-bottom: 8px;">🔗 打开 Google 授权页面</a>
                <input type="text" id="auth-code-input" class="auth-input" placeholder="粘贴授权完成后的完整网址或 code">
                <button class="btn btn-emerald btn-sm" style="width: 100%;" onclick="submitAuthCode()">提交授权码</button>
              </div>
            </div>
            <div id="auth-ready-box" style="padding: 10px; background: rgba(16, 185, 129, 0.08); border: 1px solid rgba(16, 185, 129, 0.2); border-radius: 6px; font-size: 0.85rem;">
              <div style="color: #34d399; font-weight: 600; margin-bottom: 4px;">✅ YouTube 授权凭据有效</div>
              <div style="color: var(--text-muted); font-size: 0.78rem;">可正常上传视频分片与双向云端比对同步</div>
            </div>
          </div>
        </div>

        <!-- Storage & Path Info Card -->
        <div class="card">
          <div class="card-header">
            <div class="card-title">
              <span>📁 运行环境路径</span>
            </div>
          </div>
          <div style="font-size: 0.82rem; color: var(--text-muted); display: flex; flex-direction: column; gap: 8px;">
            <div>
              <span style="color: var(--text-sub); display: block; font-size: 0.72rem;">媒体资源根目录:</span>
              <code style="color: #93c5fd;" id="info-media-dir">--</code>
            </div>
            <div>
              <span style="color: var(--text-sub); display: block; font-size: 0.72rem;">分片构建目录:</span>
              <code style="color: #93c5fd;" id="info-segments-dir">--</code>
            </div>
            <div>
              <span style="color: var(--text-sub); display: block; font-size: 0.72rem;">元数据数据库:</span>
              <code style="color: #93c5fd;" id="info-db-path">--</code>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <script>
    async function loadSummaryStats() {
      try {
        const res = await fetch('/api/media/summary');
        if (!res.ok) return;
        const d = await res.json();
        if (d.stats) {
          const s = d.stats;
          document.getElementById('stat-total-files').textContent = (s.total_files || 0).toLocaleString();
          document.getElementById('stat-total-size').textContent = '总体积: ' + (s.total_size_formatted || '0 B');
          document.getElementById('stat-photos').textContent = (s.photos_count || 0).toLocaleString();
          document.getElementById('stat-videos').textContent = (s.videos_count || 0).toLocaleString();
          document.getElementById('stat-segments').textContent = (s.total_segments || 0).toLocaleString();
          document.getElementById('stat-segments-uploaded').textContent = '已上传: ' + (s.uploaded_segments || 0).toLocaleString();
          document.getElementById('stat-date-span').textContent = s.date_span || '暂无数据';
          document.getElementById('stat-skipped').textContent = '过滤 Live Photos: ' + (s.skipped_count || 0).toLocaleString();
        }
      } catch (e) {}
    }

    async function pollDashboard() {
      try {
        const res = await fetch('/api/dashboard');
        if (!res.ok) return;
        const d = await res.json();

        // Update Paths
        if (d.media_dir) document.getElementById('info-media-dir').textContent = d.media_dir;
        if (d.segments_dir) document.getElementById('info-segments-dir').textContent = d.segments_dir;
        if (d.db_path) document.getElementById('info-db-path').textContent = d.db_path;

        // Update Auth
        const authDetails = document.getElementById('auth-details');
        const authReadyBox = document.getElementById('auth-ready-box');
        const authBadge = document.getElementById('auth-status-badge');
        if (d.auth && d.auth.required) {
          authBadge.innerHTML = '<span class="badge badge-pending">等待授权</span>';
          authDetails.style.display = 'block';
          authReadyBox.style.display = 'none';
          if (d.auth.auth_url) {
            document.getElementById('auth-link-btn').href = d.auth.auth_url;
          }
        } else {
          authBadge.innerHTML = '<span class="badge badge-sealed">已授权</span>';
          authDetails.style.display = 'none';
          authReadyBox.style.display = 'block';
        }

        // Update Task Container
        const tc = document.getElementById('task-content');
        const tb = document.getElementById('task-badge');

        const taskCards = [];
        const badges = [];

        if (d.scanner && d.scanner.is_scanning) {
          const sc = d.scanner;
          badges.push('扫描中');
          taskCards.push(`
            <div class="task-indicator" style="margin-bottom: 12px;">
              <div class="task-spinner"></div>
              <div style="flex: 1;">
                <div class="task-desc">正在扫描媒体文件</div>
                <div class="task-meta">已扫描 ${sc.count} 个文件 · ${sc.current || ''}</div>
              </div>
            </div>
          `);
        }

        if (d.transcode && d.transcode.segment_id) {
          const t = d.transcode;
          const isPaused = !!t.is_paused;
          const pct = t.total > 0 ? Math.round((t.done / t.total) * 100) : 0;
          badges.push(isPaused ? '转码已暂停' : `转码中: ${pct}%`);
          const spinner = isPaused
            ? '<span style="font-size: 1.3rem; margin-right: 4px;">⏸️</span>'
            : '<div class="task-spinner" style="border-top-color: var(--amber);"></div>';
          const btn = isPaused
            ? `<button class="btn btn-sm btn-emerald" onclick="toggleTranscodePause(false)">▶️ 继续转码</button>`
            : `<button class="btn btn-sm btn-amber" onclick="toggleTranscodePause(true)">⏸️ 暂停转码</button>`;
          const barStyle = isPaused ? 'background: #94a3b8;' : 'background: linear-gradient(90deg, #f59e0b, #fbbf24);';
          let encTag = '';
          if (t.encoder) {
            const isHw = ['nvenc', 'videotoolbox', 'qsv'].some(k => t.encoder.includes(k));
            encTag = ` · 编码器: <span style="color: ${isHw ? '#34d399' : '#f87171'}; font-weight: 500;">${t.encoder}${isHw ? ' ⚡(硬件加速)' : ' (CPU软编)'}</span>`;
          }

          taskCards.push(`
            <div class="task-indicator" style="margin-bottom: 12px; align-items: flex-start;">
              ${spinner}
              <div style="flex: 1; min-width: 0;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                  <div class="task-desc" style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${t.title || t.segment_id}">
                    ${isPaused ? '⏸️ 转码已暂停' : '⚡ 正在转码'}: ${t.title || t.segment_id}
                  </div>
                  <div style="margin-left: 10px; flex-shrink: 0;">
                    ${btn}
                  </div>
                </div>
                <div class="task-meta">切片进度: ${t.done} / ${t.total} (${pct}%) · ${t.current_file || ''}${encTag}</div>
                <div class="prog-track" style="margin-top: 8px;">
                  <div class="prog-bar" style="width: ${pct}%; ${barStyle}"></div>
                </div>
              </div>
            </div>
          `);
        }

        if (d.upload && d.upload.segment_id) {
          const u = d.upload;
          const isPaused = !!u.is_paused;
          const pct = Math.round(u.percent || 0);
          badges.push(isPaused ? '上传已暂停' : `上传中: ${pct}%`);
          const spinner = isPaused
            ? '<span style="font-size: 1.3rem; margin-right: 4px;">⏸️</span>'
            : '<div class="task-spinner" style="border-top-color: var(--purple);"></div>';
          const btn = isPaused
            ? `<button class="btn btn-sm btn-emerald" onclick="toggleUploadPause(false)">▶️ 继续上传</button>`
            : `<button class="btn btn-sm btn-amber" onclick="toggleUploadPause(true)">⏸️ 暂停上传</button>`;
          const speedText = isPaused ? '已暂停' : (u.speed || '计算中...');
          const barClass = isPaused ? '' : 'purple';
          const barStyle = isPaused ? 'background: #94a3b8;' : '';

          taskCards.push(`
            <div class="task-indicator" style="margin-bottom: 12px; align-items: flex-start;">
              ${spinner}
              <div style="flex: 1; min-width: 0;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                  <div class="task-desc" style="white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="${u.title || u.segment_id}">
                    ${isPaused ? '⏸️ 上传已暂停' : '🚀 正在上传'}: ${u.title || u.segment_id}
                  </div>
                  <div style="margin-left: 10px; flex-shrink: 0;">
                    ${btn}
                  </div>
                </div>
                <div class="task-meta">上传进度: ${pct}% · 速度: ${speedText}</div>
                <div class="prog-track" style="margin-top: 8px;">
                  <div class="prog-bar ${barClass}" style="width: ${pct}%; ${barStyle}"></div>
                </div>
              </div>
            </div>
          `);
        }

        if (taskCards.length > 0) {
          tb.textContent = badges.join(' · ');
          tc.innerHTML = taskCards.join('');
        } else {
          tb.textContent = '空闲';
          tc.innerHTML = `
            <div class="task-indicator">
              <span style="font-size: 1.4rem;">☕</span>
              <div>
                <div class="task-desc">系统处于空闲待命状态</div>
                <div class="task-meta">${d.task || '暂无活跃任务，文件变动将自动触发后台同步'}</div>
              </div>
            </div>
          `;
        }

        // Upload Queue
        const qc = document.getElementById('upload-queue-container');
        const qBadge = document.getElementById('queue-count-badge');
        const q = d.upload_queue || [];
        qBadge.textContent = `${q.length} 个排队中`;
        if (q.length === 0) {
          qc.innerHTML = '<div style="color: var(--text-muted); font-size: 0.85rem; text-align: center; padding: 20px 0;">暂无排队中的待上传分片</div>';
        } else {
          qc.innerHTML = q.map(item => `
            <div class="queue-item">
              <div>
                <span style="font-weight: 600; color: #fff;">${item.title || item.segment_id}</span>
                <span style="font-size: 0.75rem; color: var(--text-muted); margin-left: 8px;">${Math.round(item.duration_seconds || 0)}秒</span>
              </div>
              <span class="badge badge-pending">排队中</span>
            </div>
          `).join('');
        }
      } catch (e) {}
    }

    async function submitAuthCode() {
      const inp = document.getElementById('auth-code-input');
      const val = (inp.value || '').trim();
      if (!val) {
        showToast('请输入授权网址或 code', true);
        return;
      }
      try {
        const res = await fetch('/api/auth/submit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url: val })
        });
        const d = await res.json();
        if (res.ok && d.ok) {
          showToast('✅ 授权成功！');
          inp.value = '';
          pollDashboard();
        } else {
          showToast('❌ 授权失败: ' + (d.error || '验证失败'), true);
        }
      } catch (err) {
        showToast('❌ 请求失败: ' + err.message, true);
      }
    }

    async function toggleTranscodePause(pause) {
      try {
        const url = pause ? '/api/transcode/pause' : '/api/transcode/resume';
        const res = await fetch(url, { method: 'POST' });
        const d = await res.json();
        showToast(d.message || (pause ? '已暂停转码' : '已恢复转码'));
        pollDashboard();
      } catch (err) {
        showToast('❌ 操作失败: ' + err.message, true);
      }
    }

    async function toggleUploadPause(pause) {
      try {
        const url = pause ? '/api/upload/pause' : '/api/upload/resume';
        const res = await fetch(url, { method: 'POST' });
        const d = await res.json();
        showToast(d.message || (pause ? '已暂停上传' : '已恢复上传'));
        pollDashboard();
      } catch (err) {
        showToast('❌ 操作失败: ' + err.message, true);
      }
    }

    loadSummaryStats();
    pollDashboard();
    setInterval(pollDashboard, 1500);
    setInterval(loadSummaryStats, 8000);
  </script>
</body>
</html>"""

def render_dashboard_html() -> str:
    """Render full HTML for the Dashboard page."""
    navbar = render_navbar(active_page="dashboard")
    return _DASHBOARD_HTML_TEMPLATE.replace("__COMMON_CSS__", COMMON_CSS).replace("__NAVBAR__", navbar)
