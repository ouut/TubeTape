"""Segments management page for TubeTape Web UI."""

from __future__ import annotations

from .common import COMMON_CSS, render_navbar

_SEGMENTS_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>分片管理 · TubeTape</title>
  <style>
    __COMMON_CSS__

    .toolbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-wrap: wrap;
      gap: 14px;
      margin-bottom: 20px;
    }
    .toolbar-left {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .search-input {
      padding: 7px 12px;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--border);
      border-radius: 6px;
      color: #fff;
      font-size: 0.85rem;
      width: 220px;
    }
    .search-input:focus { outline: none; border-color: var(--primary); }

    /* Segment Card */
    .segment-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 10px;
      margin-bottom: 16px;
      overflow: hidden;
      transition: border-color 0.15s ease;
    }
    .segment-card:hover { border-color: var(--border-accent); }

    .segment-card-header {
      padding: 16px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      cursor: pointer;
      background: rgba(255, 255, 255, 0.02);
      transition: background 0.15s ease;
    }
    .segment-card-header:hover { background: rgba(255, 255, 255, 0.05); }

    .seg-title-row {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .seg-title {
      font-size: 1rem;
      font-weight: 600;
      color: #fff;
    }
    .seg-time-range {
      font-size: 0.8rem;
      color: var(--text-muted);
    }

    .seg-actions {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    /* Candidate section */
    .candidate-box {
      background: rgba(245, 158, 11, 0.08);
      border: 1px dashed rgba(245, 158, 11, 0.4);
      border-radius: 8px;
      padding: 12px 16px;
      margin: 14px 20px 8px 20px;
    }
    .candidate-title {
      font-size: 0.85rem;
      font-weight: 600;
      color: #fbbf24;
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 8px;
    }

    /* File Table inside segment */
    .file-table-container {
      padding: 0 20px 16px 20px;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
    }
    .file-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.82rem;
      margin-top: 12px;
    }
    .file-table th {
      text-align: left;
      padding: 8px 10px;
      color: var(--text-muted);
      border-bottom: 1px solid var(--border);
      font-weight: 600;
    }
    .file-table td {
      padding: 8px 10px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.03);
      color: #e5e7eb;
      vertical-align: middle;
    }
    .file-table tr.excluded td {
      opacity: 0.4;
      text-decoration: line-through;
    }
    .file-thumb {
      width: 44px;
      height: 32px;
      object-fit: cover;
      border-radius: 4px;
      background: #1e293b;
    }

    /* Toggle Switch */
    .toggle-switch {
      position: relative;
      display: inline-block;
      width: 36px;
      height: 20px;
    }
    .toggle-switch input { opacity: 0; width: 0; height: 0; }
    .toggle-slider {
      position: absolute;
      cursor: pointer;
      top: 0; left: 0; right: 0; bottom: 0;
      background-color: #4b5563;
      transition: .2s;
      border-radius: 20px;
    }
    .toggle-slider:before {
      position: absolute;
      content: "";
      height: 14px;
      width: 14px;
      left: 3px;
      bottom: 3px;
      background-color: white;
      transition: .2s;
      border-radius: 50%;
    }
    input:checked + .toggle-slider { background-color: var(--emerald); }
    input:checked + .toggle-slider:before { transform: translateX(16px); }
  </style>
</head>
<body>
  __NAVBAR__

  <div class="container">
    <div class="toolbar">
      <div class="toolbar-left">
        <h2 style="font-size: 1.25rem; font-weight: 700;">🎞️ 分片与文件管理</h2>
        <span class="badge badge-info" id="seg-summary-badge">0 个分片</span>
      </div>
      <div style="display: flex; gap: 10px;">
        <input type="text" id="seg-search" class="search-input" placeholder="搜索分片名称或状态..." oninput="filterSegments()">
        <button class="btn btn-amber" onclick="triggerForceReplan()">🔄 强制重新规划分段</button>
      </div>
    </div>

    <div id="segments-container">
      <div style="text-align: center; padding: 40px; color: var(--text-muted);">
        正在加载分片与文件列表...
      </div>
    </div>
  </div>

  <script>
    let allSegments = [];
    let expandedSegments = new Set();

    async function loadSegments() {
      try {
        const res = await fetch('/api/segments');
        if (!res.ok) throw new Error('拉取失败');
        const d = await res.json();
        allSegments = d.segments || [];
        document.getElementById('seg-summary-badge').textContent = `共 ${allSegments.length} 个分段`;
        renderSegmentsList();
      } catch (err) {
        document.getElementById('segments-container').innerHTML = `
          <div style="text-align: center; padding: 40px; color: #f87171;">
            加载失败: ${err.message}
          </div>
        `;
      }
    }

    function renderSegmentsList() {
      const q = (document.getElementById('seg-search').value || '').trim().toLowerCase();
      const filtered = allSegments.filter(s => {
        if (!q) return true;
        return (s.title && s.title.toLowerCase().includes(q)) ||
               (s.status && s.status.toLowerCase().includes(q)) ||
               (s.start_ts && s.start_ts.includes(q));
      });

      const c = document.getElementById('segments-container');
      if (filtered.length === 0) {
        c.innerHTML = '<div style="text-align: center; padding: 40px; color: var(--text-muted);">未检索到匹配的分段</div>';
        return;
      }

      c.innerHTML = filtered.map(seg => {
        const isExp = expandedSegments.has(seg.segment_id);
        const candidates = seg.candidate_files || [];
        const files = seg.files || [];
        const activeCount = seg.active_files_count ?? files.filter(f => !f.is_excluded).length;

        // Local status badge
        let localBadge = '';
        if (seg.transcode_progress) {
          localBadge = `<span class="badge badge-pending">⚡ 构建中 ${Math.round(seg.transcode_progress.percent)}%</span>`;
        } else if (seg.has_local_file) {
          localBadge = `<span class="badge badge-info">📁 本地已有 (${seg.local_size_formatted || '已生成'})</span>`;
        } else {
          localBadge = `<span class="badge" style="background: rgba(255,255,255,0.06); color: var(--text-muted);">⚪ 本地无文件</span>`;
        }

        // Cloud status badge
        let cloudBadge = '';
        if (seg.upload_progress) {
          cloudBadge = `<span class="badge badge-pending">🚀 上传中 ${Math.round(seg.upload_progress.percent)}% · ${seg.upload_progress.speed}</span>`;
        } else if (seg.is_upload_queued) {
          cloudBadge = `<span class="badge badge-info">🕒 排队上传中</span>`;
        } else if (seg.youtube_video_id) {
          cloudBadge = `<a href="https://youtu.be/${seg.youtube_video_id}" target="_blank" class="badge badge-sealed" style="text-decoration: none;">▶️ YouTube (${seg.youtube_video_id})</a>`;
        } else {
          cloudBadge = `<span class="badge" style="background: rgba(255,255,255,0.06); color: var(--text-muted);">⚪ 云端未上传</span>`;
        }

        const oversizedBadge = seg.is_oversized ? '<span class="badge badge-failed" title="当前有效文件总时长超过分段时长上限">⚠️ 时长超限</span>' : '';

        return `
          <div class="segment-card" id="card-${seg.segment_id}">
            <div class="segment-card-header" onclick="toggleExpand('${seg.segment_id}')">
              <div style="flex: 1; min-width: 0;">
                <div class="seg-title-row">
                  <span style="font-size: 1.1rem; color: #60a5fa;">${isExp ? '▼' : '▶'}</span>
                  <span class="seg-title">${seg.title}</span>
                  ${localBadge}
                  ${cloudBadge}
                  ${oversizedBadge}
                </div>
                <div class="seg-time-range" style="margin-top: 4px; margin-left: 26px;">
                  拍摄区间: ${seg.start_ts ? seg.start_ts.slice(0, 10) : '--'} ~ ${seg.end_ts ? seg.end_ts.slice(0, 10) : '--'} · 时长: ${Math.round(seg.duration_seconds || 0)}秒 · 有效文件: ${activeCount} / ${files.length}
                </div>

                ${seg.transcode_progress ? `
                  <div style="margin: 8px 0 0 26px; font-size: 0.75rem; color: #93c5fd;" onclick="event.stopPropagation()">
                    <div style="display:flex; justify-content:space-between; margin-bottom: 3px;">
                      <span>⚡ 正在构建: ${seg.transcode_progress.current_file || ''}</span>
                      <span>${seg.transcode_progress.done_files || 0} / ${seg.transcode_progress.total_files || 0} (${Math.round(seg.transcode_progress.percent)}%)</span>
                    </div>
                    <div style="height: 4px; background: rgba(255,255,255,0.1); border-radius: 2px; overflow: hidden;">
                      <div style="height: 100%; width: ${seg.transcode_progress.percent}%; background: #3b82f6; transition: width 0.3s ease;"></div>
                    </div>
                  </div>
                ` : ''}

                ${seg.upload_progress ? `
                  <div style="margin: 8px 0 0 26px; font-size: 0.75rem; color: #a7f3d0;" onclick="event.stopPropagation()">
                    <div style="display:flex; justify-content:space-between; margin-bottom: 3px;">
                      <span>🚀 正在上传至 YouTube · 速度: ${seg.upload_progress.speed || '计算中...'}</span>
                      <span>${Math.round(seg.upload_progress.percent)}%</span>
                    </div>
                    <div style="height: 4px; background: rgba(255,255,255,0.1); border-radius: 2px; overflow: hidden;">
                      <div style="height: 100%; width: ${seg.upload_progress.percent}%; background: #10b981; transition: width 0.3s ease;"></div>
                    </div>
                  </div>
                ` : ''}
              </div>

              <div class="seg-actions" onclick="event.stopPropagation()">
                ${seg.is_oversized ? `<button class="btn btn-sm btn-amber" onclick="splitSegment('${seg.segment_id}')">✂️ 超限拆分</button>` : ''}
                ${seg.has_local_file ? `
                  <button class="btn btn-sm btn-danger" onclick="deleteLocalSegment('${seg.segment_id}')" title="删除本地 .mp4 视频释放磁盘空间">🗑️ 删本地</button>
                  <button class="btn btn-sm btn-emerald" onclick="rebuildSegment('${seg.segment_id}')">🔄 重新构建</button>
                ` : `
                  <button class="btn btn-sm btn-emerald" onclick="rebuildSegment('${seg.segment_id}')">🔨 构建视频</button>
                `}
                ${seg.youtube_video_id ? `
                  <button class="btn btn-sm btn-danger" onclick="deleteYoutubeVideo('${seg.segment_id}')" title="从 YouTube 删除视频并解绑">🗑️ 删云端</button>
                ` : (seg.has_local_file ? `
                  <button class="btn btn-sm btn-sync" onclick="queueUpload('${seg.segment_id}')">☁️ 立即上传</button>
                ` : '')}
              </div>
            </div>

            <div id="detail-${seg.segment_id}" style="display: ${isExp ? 'block' : 'none'};">
              ${candidates.length > 0 ? `
                <div class="candidate-box">
                  <div class="candidate-title">
                    <span>✨ 发现 ${candidates.length} 个属于此时间段的新文件 (未分配分段)</span>
                  </div>
                  <div style="display: flex; flex-direction: column; gap: 8px;">
                    ${candidates.map(cand => `
                      <div style="display: flex; align-items: center; justify-content: space-between; background: rgba(0,0,0,0.25); padding: 6px 12px; border-radius: 6px; font-size: 0.8rem;">
                        <div style="display: flex; align-items: center; gap: 10px;">
                          <img class="file-thumb" src="/api/media/stream?id=${encodeURIComponent(cand.file_id)}">
                          <div>
                            <span style="font-weight: 600; color: #fff;">${cand.name}</span>
                            <span style="color: var(--text-muted); margin-left: 8px;">${cand.captured_at_utc.slice(0, 19)}</span>
                          </div>
                        </div>
                        <button class="btn btn-sm btn-emerald" onclick="includeCandidateFile('${seg.segment_id}', '${cand.file_id}')">➕ 纳入此分段</button>
                      </div>
                    `).join('')}
                  </div>
                </div>
              ` : ''}

              <div class="file-table-container">
                <table class="file-table">
                  <thead>
                    <tr>
                      <th style="width: 50px;">构建</th>
                      <th style="width: 60px;">预览</th>
                      <th>文件名 / 路径</th>
                      <th>类型</th>
                      <th>拍摄时间</th>
                      <th>规格 / 时长</th>
                      <th>大小</th>
                    </tr>
                  </thead>
                  <tbody>
                    ${files.map(f => `
                      <tr class="${f.is_excluded ? 'excluded' : ''}">
                        <td>
                          <label class="toggle-switch">
                            <input type="checkbox" ${!f.is_excluded ? 'checked' : ''} onchange="toggleFileExclusion('${seg.segment_id}', '${f.file_id}', !this.checked)">
                            <span class="toggle-slider"></span>
                          </label>
                        </td>
                        <td>
                          <img class="file-thumb" src="/api/media/stream?id=${encodeURIComponent(f.file_id)}">
                        </td>
                        <td>
                          <div style="font-weight: 500;">${f.name}</div>
                          <div style="font-size: 0.72rem; color: var(--text-sub);">${f.path}</div>
                        </td>
                        <td>${f.type === 'video' ? '🎥 视频' : '📸 照片'}</td>
                        <td>${f.captured_at_utc ? f.captured_at_utc.slice(0, 19).replace('T', ' ') : '--'}</td>
                        <td>${f.type === 'video' ? Math.round(f.duration_seconds || 0) + 's' : (f.resolution || '--')}</td>
                        <td>${(f.size_bytes / (1024*1024)).toFixed(1)} MB</td>
                      </tr>
                    `).join('')}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        `;
      }).join('');
    }

    function toggleExpand(segId) {
      if (expandedSegments.has(segId)) {
        expandedSegments.delete(segId);
      } else {
        expandedSegments.add(segId);
      }
      renderSegmentsList();
    }

    function filterSegments() {
      renderSegmentsList();
    }

    async function toggleFileExclusion(segmentId, fileId, isExcluded) {
      try {
        const res = await fetch('/api/segment/toggle_file', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ segment_id: segmentId, file_id: fileId, excluded: isExcluded })
        });
        const d = await res.json();
        if (res.ok && d.ok) {
          showToast(isExcluded ? '已排除该文件（不参与分片构建）' : '已重新包含该文件');
          loadSegments();
        } else {
          showToast('❌ 更新失败: ' + (d.error || '未知错误'), true);
        }
      } catch (err) {
        showToast('❌ 请求失败: ' + err.message, true);
      }
    }

    async function includeCandidateFile(segmentId, fileId) {
      try {
        const res = await fetch('/api/segment/include_file', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ segment_id: segmentId, file_id: fileId })
        });
        const d = await res.json();
        if (res.ok && d.ok) {
          showToast('✅ ' + (d.message || '文件已纳入分片'));
          loadSegments();
        } else {
          showToast('❌ 操作失败: ' + (d.error || '未知错误'), true);
        }
      } catch (err) {
        showToast('❌ 请求失败: ' + err.message, true);
      }
    }

    async function rebuildSegment(id) {
      if (!confirm(`确定要重新构建分段 [${id.slice(0, 12)}] 吗？`)) return;
      try {
        const res = await fetch('/api/segment/rebuild', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ segment_id: id })
        });
        const d = await res.json();
        if (res.ok && d.ok) {
          showToast('✅ ' + (d.message || '重新构建任务已启动'));
        } else {
          showToast('❌ 启动失败: ' + (d.error || '未知错误'), true);
        }
      } catch (err) {
        showToast('❌ 请求失败: ' + err.message, true);
      }
    }

    async function queueUpload(id) {
      try {
        const res = await fetch('/api/segment/upload', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ segment_id: id })
        });
        const d = await res.json();
        if (res.ok && d.ok) {
          showToast('✅ ' + (d.message || '已加入上传队列'));
        } else {
          showToast('❌ 加入失败: ' + (d.error || '未知错误'), true);
        }
      } catch (err) {
        showToast('❌ 请求失败: ' + err.message, true);
      }
    }

    async function triggerForceReplan() {
      if (!confirm('确定要强制重新规划所有分段吗？这会根据最新文件和配置重新分段。')) return;
      try {
        const res = await fetch('/api/segments/force_replan', { method: 'POST' });
        const d = await res.json();
        if (res.ok && d.ok) {
          showToast('✅ ' + (d.message || '重新规划任务已启动'));
          setTimeout(loadSegments, 1500);
        } else {
          showToast('❌ 启动失败: ' + (d.error || '未知错误'), true);
        }
      } catch (err) {
        showToast('❌ 请求失败: ' + err.message, true);
      }
    async function deleteLocalSegment(id) {
      if (!confirm(`确定要删除分段 [${id.slice(0, 12)}] 的本地视频文件吗？\n这会释放磁盘空间，原始照片文件与 YouTube 云端视频不受影响。`)) return;
      try {
        const res = await fetch('/api/segment/delete_local', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ segment_id: id })
        });
        const d = await res.json();
        if (res.ok && d.ok) {
          showToast('✅ ' + (d.message || '本地视频已删除'));
          loadSegments();
        } else {
          showToast('❌ 删除失败: ' + (d.error || '未知错误'), true);
        }
      } catch (err) {
        showToast('❌ 请求失败: ' + err.message, true);
      }
    }

    async function deleteYoutubeVideo(id) {
      if (!confirm(`⚠️ 危险操作：确定要从 YouTube 上删除此分段对应的视频吗？\n删除后该视频在 YouTube 上将无法找回！`)) return;
      try {
        const res = await fetch('/api/segment/delete_youtube', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ segment_id: id })
        });
        const d = await res.json();
        if (res.ok && d.ok) {
          showToast('✅ ' + (d.message || '已从 YouTube 成功删除'));
          loadSegments();
        } else {
          showToast('❌ 删除失败: ' + (d.error || '未知错误'), true);
        }
      } catch (err) {
        showToast('❌ 请求失败: ' + err.message, true);
      }
    }

    async function splitSegment(id) {
      if (!confirm(`确定要将超长分段 [${id.slice(0, 12)}] 拆分为多个符合时长限制的新分段吗？`)) return;
      try {
        const res = await fetch('/api/segment/split', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ segment_id: id })
        });
        const d = await res.json();
        if (res.ok && d.ok) {
          showToast('✅ ' + (d.message || '分段拆分成功'));
          loadSegments();
        } else {
          showToast('❌ 拆分失败: ' + (d.error || '未知错误'), true);
        }
      } catch (err) {
        showToast('❌ 请求失败: ' + err.message, true);
      }
    }

    loadSegments();
    setInterval(loadSegments, 2500);
  </script>
</body>
</html>"""

def render_segments_html() -> str:
    """Render full HTML for the Segments management page."""
    navbar = render_navbar(active_page="segments")
    return _SEGMENTS_HTML_TEMPLATE.replace("__COMMON_CSS__", COMMON_CSS).replace("__NAVBAR__", navbar)
