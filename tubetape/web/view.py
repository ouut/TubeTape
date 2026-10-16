"""Immersive 2D gallery viewer for TubeTape Web UI."""

from __future__ import annotations


def render_view_html() -> str:
    """Render full HTML for the clean 2D navigation View page."""
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
  <title>视界画廊 · TubeTape</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body, html {
      width: 100%;
      height: 100%;
      background: #000;
      color: #fff;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      overflow: hidden;
      user-select: none;
    }

    /* Floating Minimalist Top Header */
    .view-header {
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 60px;
      padding: 0 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: linear-gradient(180deg, rgba(0,0,0,0.85) 0%, rgba(0,0,0,0.4) 70%, transparent 100%);
      z-index: 100;
      pointer-events: auto;
      transition: opacity 0.3s ease;
    }
    .view-header.autohide { opacity: 0; pointer-events: none; }

    .header-left {
      display: flex;
      align-items: center;
      gap: 16px;
    }
    .btn-back {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.12);
      border: 1px solid rgba(255, 255, 255, 0.2);
      color: #fff;
      text-decoration: none;
      font-size: 0.85rem;
      font-weight: 500;
      backdrop-filter: blur(8px);
      transition: background 0.15s ease;
    }
    .btn-back:hover { background: rgba(255, 255, 255, 0.25); }

    .segment-meta {
      display: flex;
      flex-direction: column;
    }
    .segment-title {
      font-size: 1rem;
      font-weight: 600;
      color: #fff;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .segment-sub {
      font-size: 0.75rem;
      color: #9ca3af;
    }

    .header-right {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .item-counter {
      font-size: 0.85rem;
      font-weight: 600;
      padding: 4px 10px;
      background: rgba(255, 255, 255, 0.12);
      border-radius: 20px;
      border: 1px solid rgba(255, 255, 255, 0.15);
      backdrop-filter: blur(8px);
    }

    /* Minimalist Side Navigation Indicators */
    .nav-arrow {
      position: absolute;
      top: 50%;
      transform: translateY(-50%);
      width: 44px;
      height: 44px;
      border-radius: 50%;
      background: rgba(0, 0, 0, 0.4);
      border: 1px solid rgba(255, 255, 255, 0.15);
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 1.5rem;
      cursor: pointer;
      z-index: 50;
      backdrop-filter: blur(6px);
      transition: all 0.2s ease;
      opacity: 0.35;
    }
    .nav-arrow:hover {
      opacity: 1;
      background: rgba(0, 0, 0, 0.7);
      transform: translateY(-50%) scale(1.08);
    }
    .nav-arrow.left { left: 16px; }
    .nav-arrow.right { right: 16px; }

    /* Media Display Stage */
    .stage {
      width: 100%;
      height: 100%;
      position: relative;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .media-element {
      max-width: 100%;
      max-height: 100%;
      object-fit: contain;
      box-shadow: 0 0 40px rgba(0, 0, 0, 0.8);
      transition: opacity 0.25s ease;
    }

    /* Bottom Floating File Info */
    .file-caption {
      position: absolute;
      bottom: 20px;
      left: 50%;
      transform: translateX(-50%);
      background: rgba(15, 23, 42, 0.75);
      backdrop-filter: blur(10px);
      border: 1px solid rgba(255, 255, 255, 0.1);
      padding: 6px 16px;
      border-radius: 20px;
      font-size: 0.8rem;
      color: #e2e8f0;
      pointer-events: none;
      z-index: 90;
      display: flex;
      align-items: center;
      gap: 12px;
      transition: opacity 0.3s ease;
    }
    .file-caption.autohide { opacity: 0; }

    /* Help Floating Hint */
    .hint-pill {
      position: absolute;
      bottom: 60px;
      left: 50%;
      transform: translateX(-50%);
      font-size: 0.75rem;
      color: rgba(255, 255, 255, 0.5);
      background: rgba(0, 0, 0, 0.5);
      padding: 4px 12px;
      border-radius: 12px;
      pointer-events: none;
      animation: fadeOutHint 6s forwards;
    }
    @keyframes fadeOutHint {
      0%, 70% { opacity: 1; }
      100% { opacity: 0; }
    }

    /* Empty state */
    .empty-state {
      text-align: center;
      color: #9ca3af;
      padding: 40px;
    }
  </style>
</head>
<body>
  <!-- Header -->
  <header class="view-header" id="view-header">
    <div class="header-left">
      <a href="/" class="btn-back">‹ 返回控制台</a>
      <div class="segment-meta">
        <div class="segment-title" id="seg-title">正在加载画廊...</div>
        <div class="segment-sub" id="seg-sub">左右滑动切分片 · 上下滑动选文件</div>
      </div>
    </div>
    <div class="header-right">
      <div class="item-counter" id="item-counter">0 / 0</div>
    </div>
  </header>

  <!-- Left / Right arrows for Segment switching -->
  <button class="nav-arrow left" id="btn-prev-seg" onclick="switchSegment(-1)" title="上一个分片 (←)">‹</button>
  <button class="nav-arrow right" id="btn-next-seg" onclick="switchSegment(1)" title="下一个分片 (→)">›</button>

  <!-- Media Display Area -->
  <main class="stage" id="stage" onclick="toggleHeaderVisibility()">
    <div class="empty-state" id="empty-state">正在拉取媒体资源列表...</div>
  </main>

  <!-- File caption badge -->
  <div class="file-caption" id="file-caption">
    <span id="caption-name">--</span>
    <span style="opacity: 0.4;">|</span>
    <span id="caption-time">--</span>
    <span style="opacity: 0.4;">|</span>
    <span id="caption-meta">--</span>
  </div>

  <div class="hint-pill">💡 提示：左右滑动/方向键切换分片，上下滑动/滚轮切换文件</div>

  <script>
    let segments = [];
    let currentSegIndex = 0;
    let currentItemIndex = 0;
    let headerVisible = true;
    let hideTimer = null;

    async function loadGalleryData() {
      try {
        const res = await fetch('/api/segments');
        if (!res.ok) throw new Error('拉取分段失败');
        const data = await res.json();
        segments = data.segments || [];

        if (segments.length === 0) {
          // Fallback to planned segments or scan
          const res2 = await fetch('/api/media/items?page=1&page_size=100');
          const d2 = await res2.json();
          if (d2.items && d2.items.length > 0) {
            segments = [{
              segment_id: 'default',
              title: '全量相册',
              files: d2.items
            }];
          }
        }

        if (segments.length === 0) {
          document.getElementById('empty-state').textContent = '未检索到任何相册照片或视频';
          return;
        }

        currentSegIndex = 0;
        currentItemIndex = 0;
        renderCurrent();
      } catch (err) {
        document.getElementById('empty-state').textContent = '加载失败: ' + err.message;
      }
    }

    function renderCurrent() {
      if (!segments || segments.length === 0) return;
      const seg = segments[currentSegIndex];
      const files = (seg.files || []).filter(f => !f.is_excluded);

      document.getElementById('seg-title').textContent = `${seg.title || '分段 ' + (currentSegIndex + 1)} (${currentSegIndex + 1}/${segments.length})`;
      document.getElementById('seg-sub').textContent = seg.start_ts ? `${seg.start_ts.slice(0, 10)} ~ ${seg.end_ts.slice(0, 10)}` : '未命名时间段';

      if (!files || files.length === 0) {
        document.getElementById('stage').innerHTML = '<div class="empty-state">本分片内暂无有效媒体文件</div>';
        document.getElementById('item-counter').textContent = '0 / 0';
        document.getElementById('file-caption').style.display = 'none';
        return;
      }

      if (currentItemIndex >= files.length) currentItemIndex = files.length - 1;
      if (currentItemIndex < 0) currentItemIndex = 0;

      const item = files[currentItemIndex];
      document.getElementById('item-counter').textContent = `${currentItemIndex + 1} / ${files.length}`;
      document.getElementById('file-caption').style.display = 'flex';
      document.getElementById('caption-name').textContent = item.name || item.path;
      document.getElementById('caption-time').textContent = (item.captured_at_utc || '').slice(0, 19).replace('T', ' ');
      document.getElementById('caption-meta').textContent = item.type === 'video'
        ? `🎥 视频 · ${Math.round(item.duration_seconds || 0)}s`
        : `📸 照片 · ${item.resolution || ''}`;

      const stage = document.getElementById('stage');
      const mediaUrl = `/api/media/stream?id=${encodeURIComponent(item.file_id)}`;

      if (item.type === 'video') {
        stage.innerHTML = `
          <video class="media-element" src="${mediaUrl}" controls autoplay loop playsinline>
            您的浏览器不支持视频播放
          </video>
        `;
      } else {
        stage.innerHTML = `
          <img class="media-element" src="${mediaUrl}" alt="${item.name}">
        `;
      }

      resetAutoHide();
    }

    function switchSegment(delta) {
      if (!segments || segments.length === 0) return;
      let newIdx = currentSegIndex + delta;
      if (newIdx < 0) newIdx = segments.length - 1;
      if (newIdx >= segments.length) newIdx = 0;
      currentSegIndex = newIdx;
      currentItemIndex = 0;
      renderCurrent();
    }

    function switchFile(delta) {
      if (!segments || segments.length === 0) return;
      const seg = segments[currentSegIndex];
      const files = (seg.files || []).filter(f => !f.is_excluded);
      if (!files || files.length === 0) return;

      let newIdx = currentItemIndex + delta;
      if (newIdx < 0) newIdx = files.length - 1;
      if (newIdx >= files.length) newIdx = 0;
      currentItemIndex = newIdx;
      renderCurrent();
    }

    // Keyboard navigation (Left/Right: Segment, Up/Down: File)
    window.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowLeft') {
        switchSegment(-1);
      } else if (e.key === 'ArrowRight') {
        switchSegment(1);
      } else if (e.key === 'ArrowUp') {
        switchFile(-1);
      } else if (e.key === 'ArrowDown') {
        switchFile(1);
      }
    });

    // Mouse wheel (Up/Down: switch file with debounce)
    let wheelTimeout = null;
    window.addEventListener('wheel', (e) => {
      if (wheelTimeout) return;
      wheelTimeout = setTimeout(() => { wheelTimeout = null; }, 180);
      if (e.deltaY > 20) {
        switchFile(1);
      } else if (e.deltaY < -20) {
        switchFile(-1);
      }
    }, { passive: true });

    // Touch gesture swipe detection (Horizontal -> segment, Vertical -> file)
    let touchStartX = 0;
    let touchStartY = 0;
    window.addEventListener('touchstart', (e) => {
      touchStartX = e.changedTouches[0].screenX;
      touchStartY = e.changedTouches[0].screenY;
    }, { passive: true });

    window.addEventListener('touchend', (e) => {
      const diffX = e.changedTouches[0].screenX - touchStartX;
      const diffY = e.changedTouches[0].screenY - touchStartY;
      const absX = Math.abs(diffX);
      const absY = Math.abs(diffY);

      if (Math.max(absX, absY) < 35) return; // ignore taps

      if (absX > absY) {
        // Horizontal swipe -> Segment
        if (diffX < 0) switchSegment(1);
        else switchSegment(-1);
      } else {
        // Vertical swipe -> File
        if (diffY < 0) switchFile(1);
        else switchFile(-1);
      }
    }, { passive: true });

    function toggleHeaderVisibility() {
      headerVisible = !headerVisible;
      const h = document.getElementById('view-header');
      const c = document.getElementById('file-caption');
      if (headerVisible) {
        h.classList.remove('autohide');
        c.classList.remove('autohide');
        resetAutoHide();
      } else {
        h.classList.add('autohide');
        c.classList.add('autohide');
      }
    }

    function resetAutoHide() {
      clearTimeout(hideTimer);
      const h = document.getElementById('view-header');
      const c = document.getElementById('file-caption');
      h.classList.remove('autohide');
      c.classList.remove('autohide');
      headerVisible = true;
      hideTimer = setTimeout(() => {
        h.classList.add('autohide');
        c.classList.add('autohide');
        headerVisible = false;
      }, 5000);
    }

    loadGalleryData();
  </script>
</body>
</html>"""
