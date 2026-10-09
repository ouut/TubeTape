"""Lightweight HTTP server for timeline viewer, dashboard, live logs, and OAuth callbacks.

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
    "media_dir": None,
    "db": None,
    "db_path": None,
    "keep_segments": 0,
    "scanner": {
        "is_scanning": False,
        "count": 0,
        "current": None,
    },
    "transcode": {
        "segment_id": None,
        "title": None,
        "done": 0,
        "total": 0,
        "current_file": None,
    },
    "planned_segments": [],
    "coordinator": None,
    "_file_cache": None,
    "_file_cache_db_len": -1,
    "_date_groups_cache": None,
}


def set_app_coordinator(coordinator) -> None:
    """Set active AppCoordinator instance."""
    _server_state["coordinator"] = coordinator


def get_app_coordinator():
    """Get active AppCoordinator instance if registered."""
    return _server_state.get("coordinator")


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


def set_web_context(
    media_dir: str | None = None,
    db=None,
    db_path: str | None = None,
    keep_segments: int = 0,
) -> None:
    """Set media directory, database instance/path, and keep_segments limit."""
    if media_dir:
        _server_state["media_dir"] = os.path.abspath(media_dir)
    if db is not None:
        _server_state["db"] = db
        _server_state["_file_cache"] = None
        _server_state["_date_groups_cache"] = None
    if db_path:
        _server_state["db_path"] = os.path.abspath(db_path)
    _server_state["keep_segments"] = max(0, keep_segments)


def update_web_scanner(is_scanning: bool, count: int = 0, current: str | None = None) -> None:
    """Update scanner state for real-time progress display in dashboard."""
    _server_state["scanner"]["is_scanning"] = is_scanning
    _server_state["scanner"]["count"] = count
    if current is not None:
        _server_state["scanner"]["current"] = current
    elif not is_scanning:
        _server_state["scanner"]["current"] = None


def set_web_planned_segments(segments: list) -> None:
    """Register the planned segments for the current run."""
    _server_state["planned_segments"] = list(segments)


def update_web_transcode(
    segment_id: str,
    title: str,
    done: int,
    total: int,
    current_file: str,
) -> None:
    """Update transcode progress for real-time progress display in dashboard."""
    _server_state["transcode"] = {
        "segment_id": segment_id,
        "title": title,
        "done": done,
        "total": total,
        "current_file": current_file,
    }


def finish_web_segment(segment_id: str, youtube_video_id: str | None = None) -> None:
    """Mark a segment as finished transcoding / uploading."""
    if _server_state["transcode"].get("segment_id") == segment_id:
        _server_state["transcode"] = {
            "segment_id": None,
            "title": None,
            "done": 0,
            "total": 0,
            "current_file": None,
        }


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


def get_db():
    """Get active Database object or attempt to load from db_path."""
    db = _server_state.get("db")
    if db is not None:
        return db
    db_path = _server_state.get("db_path")
    if db_path and os.path.exists(db_path):
        from .db import Database

        try:
            db = Database.load(db_path)
            _server_state["db"] = db
            return db
        except Exception as exc:
            _logger.debug("could not load db from %s: %s", db_path, exc)
            return None
    return None


def _get_sorted_media_files() -> list[dict]:
    """Return all media files in chronological order (earliest first)."""
    db = get_db()
    if not db or not db.files:
        return []

    cache = _server_state.get("_file_cache")
    if cache is not None and _server_state.get("_file_cache_db_len") == len(db.files):
        return cache

    file_list = []
    for fid, f in db.files.items():
        file_list.append(
            {
                "id": fid,
                "name": f.get("name") or os.path.basename(f.get("path", "")),
                "rel_path": f.get("path") or "",
                "type": f.get("type", "image"),
                "captured_at": f.get("captured_at_utc") or "",
                "resolution": f.get("resolution") or "",
                "duration": float(f.get("duration_seconds") or 0.0),
                "size": int(f.get("size_bytes") or 0),
            }
        )

    def sort_key(item: dict):
        cap = item["captured_at"]
        return (0 if cap else 1, cap, item["rel_path"])

    file_list.sort(key=sort_key)
    _server_state["_file_cache"] = file_list
    _server_state["_file_cache_db_len"] = len(db.files)
    _server_state["_date_groups_cache"] = None
    return file_list


def _resolve_media_path(file_id: str) -> tuple[str | None, dict | None]:
    """Safely resolve file_id to an absolute path within media_dir."""
    db = get_db()
    if not db or file_id not in db.files:
        return None, None

    media_dir = _server_state.get("media_dir")
    if not media_dir:
        db_path = _server_state.get("db_path")
        if db_path:
            media_dir = os.path.dirname(db_path)
        else:
            return None, None

    file_info = db.files[file_id]
    rel_path = file_info.get("path")
    if not rel_path:
        return None, None

    abs_path = os.path.abspath(os.path.join(media_dir, rel_path))
    if not abs_path.startswith(os.path.abspath(media_dir)):
        return None, None

    return abs_path, file_info


def _format_seconds(sec: float) -> str:
    s = int(sec)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


# ---------------------------------------------------------------------- HTML Pages

_TIMELINE_VIEWER_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <title>TubeTape 时间线全屏画廊</title>
  <style>
    :root {
      --bg: #000;
      --text: #fff;
      --card-bg: rgba(20, 20, 24, 0.75);
      --border: rgba(255, 255, 255, 0.15);
      --accent: #e50914;
      --blue: #3b82f6;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; user-select: none; -webkit-user-select: none; }
    html, body {
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      touch-action: none;
    }
    #viewport {
      position: absolute;
      top: 0; left: 0; right: 0; bottom: 0;
      overflow: hidden;
    }
    .slide-layer {
      position: absolute;
      top: 0; left: 0; width: 100%; height: 100%;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: transform 0.32s cubic-bezier(0.22, 1, 0.36, 1);
      will-change: transform;
    }
    .media-box {
      width: 100%;
      height: 100%;
      display: flex;
      align-items: center;
      justify-content: center;
      position: relative;
      overflow: hidden;
    }
    .media-box img {
      max-width: 100%;
      max-height: 100%;
      object-fit: contain;
      pointer-events: auto;
      transform-origin: center center;
      transition: transform 0.15s ease-out;
      cursor: zoom-in;
    }
    .media-box img.zoomed {
      cursor: grab;
    }
    .media-box img.dragging {
      cursor: grabbing;
      transition: none;
    }
    .media-box video {
      max-width: 100%;
      max-height: 100%;
      object-fit: contain;
      outline: none;
      background: #000;
    }
    /* Top Bar */
    header {
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 56px;
      padding: 0 16px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: linear-gradient(180deg, rgba(0,0,0,0.85) 0%, rgba(0,0,0,0) 100%);
      z-index: 50;
      pointer-events: auto;
    }
    .header-brand {
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 700;
      font-size: 1.1rem;
    }
    .header-brand span {
      background: var(--accent);
      color: #fff;
      padding: 2px 7px;
      border-radius: 4px;
      font-size: 0.8rem;
    }
    .header-links {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .btn-header {
      background: rgba(255, 255, 255, 0.16);
      backdrop-filter: blur(8px);
      border: 1px solid var(--border);
      color: #fff;
      padding: 6px 14px;
      border-radius: 20px;
      text-decoration: none;
      font-size: 0.84rem;
      font-weight: 500;
      display: flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
      transition: background 0.2s;
    }
    .btn-header:hover {
      background: rgba(255, 255, 255, 0.28);
    }
    /* Bottom Info Overlay */
    .bottom-overlay {
      position: absolute;
      bottom: 0; left: 0; right: 0;
      padding: 24px 20px 20px 20px;
      background: linear-gradient(0deg, rgba(0,0,0,0.88) 0%, rgba(0,0,0,0) 100%);
      z-index: 50;
      pointer-events: none;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .info-date {
      font-size: 1.1rem;
      font-weight: 700;
      color: #fff;
      text-shadow: 0 1px 4px rgba(0,0,0,0.8);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .info-badge {
      background: rgba(59, 130, 246, 0.35);
      border: 1px solid #3b82f6;
      color: #93c5fd;
      font-size: 0.72rem;
      padding: 1px 6px;
      border-radius: 4px;
      font-weight: 600;
    }
    .info-meta {
      font-size: 0.84rem;
      color: #d1d5db;
      text-shadow: 0 1px 3px rgba(0,0,0,0.8);
      display: flex;
      gap: 12px;
      align-items: center;
    }
    .info-counter {
      color: #9ca3af;
      font-size: 0.82rem;
    }
    /* Video progress line */
    .video-progress-container {
      position: absolute;
      bottom: 0; left: 0; right: 0;
      height: 4px;
      background: rgba(255, 255, 255, 0.2);
      z-index: 55;
      cursor: pointer;
      pointer-events: auto;
    }
    .video-progress-bar {
      height: 100%;
      width: 0%;
      background: var(--accent);
      transition: width 0.1s linear;
    }
    /* Right side Timeline Scrubber */
    .timeline-scrubber {
      position: absolute;
      right: 14px;
      top: 15%;
      bottom: 15%;
      width: 32px;
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 60;
      pointer-events: auto;
    }
    .scrubber-track {
      width: 4px;
      height: 100%;
      background: rgba(255, 255, 255, 0.25);
      border-radius: 2px;
      position: relative;
      cursor: pointer;
    }
    .scrubber-handle {
      position: absolute;
      left: 50%;
      width: 16px;
      height: 16px;
      background: #fff;
      border-radius: 50%;
      transform: translate(-50%, -50%);
      box-shadow: 0 0 8px rgba(0, 0, 0, 0.8);
      cursor: grab;
      touch-action: none;
    }
    .scrubber-tooltip {
      position: absolute;
      right: 42px;
      background: rgba(18, 18, 22, 0.92);
      border: 1px solid var(--border);
      color: #fff;
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 0.85rem;
      white-space: nowrap;
      pointer-events: none;
      display: none;
      box-shadow: 0 4px 14px rgba(0, 0, 0, 0.5);
      transform: translateY(-50%);
    }
    /* Floating action buttons */
    .floating-nav {
      position: absolute;
      right: 20px;
      bottom: 80px;
      display: flex;
      flex-direction: column;
      gap: 12px;
      z-index: 55;
    }
    .fab-btn {
      width: 44px;
      height: 44px;
      border-radius: 50%;
      background: rgba(25, 25, 30, 0.7);
      backdrop-filter: blur(10px);
      border: 1px solid var(--border);
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      font-size: 1.15rem;
      transition: background 0.15s, transform 0.15s;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
    }
    .fab-btn:active {
      transform: scale(0.92);
    }
    .fab-btn:hover {
      background: rgba(45, 45, 52, 0.85);
    }
    /* Empty State */
    .empty-state {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 100%;
      gap: 16px;
      text-align: center;
      padding: 20px;
    }
    .empty-icon { font-size: 3rem; animation: bounce 2s infinite; }
    @keyframes bounce { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-8px); } }
    .empty-title { font-size: 1.3rem; font-weight: 700; }
    .empty-desc { color: #9ca3af; font-size: 0.92rem; max-width: 420px; line-height: 1.5; }
    .loading-spinner {
      width: 40px; height: 40px;
      border: 3px solid rgba(255, 255, 255, 0.15);
      border-top-color: var(--blue);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }
    @keyframes spin { 100% { transform: rotate(360deg); } }
  </style>
</head>
<body>
  <div id="viewport">
    <div id="layer-prev" class="slide-layer" style="transform: translateY(-100%);">
      <div class="media-box" id="box-prev"></div>
    </div>
    <div id="layer-curr" class="slide-layer" style="transform: translateY(0);">
      <div class="media-box" id="box-curr"></div>
    </div>
    <div id="layer-next" class="slide-layer" style="transform: translateY(100%);">
      <div class="media-box" id="box-next"></div>
    </div>
  </div>

  <header>
    <div class="header-brand">
      <span>TubeTape</span>
      <div>画廊</div>
    </div>
    <div class="header-segment" style="display:flex;align-items:center;gap:6px;">
      <button id="prev-seg-btn" class="btn-header" onclick="stepSegment(-1)" title="切换至上一分片">◀</button>
      <select id="segment-select" class="btn-header" style="max-width:320px;cursor:pointer;background:rgba(20,20,26,0.9);outline:none;font-weight:600;" onchange="onSegmentSelect(this.value)">
        <option value="">载入分片中...</option>
      </select>
      <button id="next-seg-btn" class="btn-header" onclick="stepSegment(1)" title="切换至下一分片">▶</button>
      <a id="yt-seg-link" href="#" target="_blank" class="btn-header" style="display:none;background:rgba(220,38,38,0.85);border-color:#ef4444;" title="在 YouTube 上播放此分片视频">▶️ YouTube</a>
    </div>
    <div class="header-links">
      <button id="sound-btn" class="btn-header" onclick="toggleMute()">🔇 静音</button>
      <button id="zoom-btn" class="btn-header" onclick="toggleZoomCurr()">🔍 放大</button>
      <a href="/log" class="btn-header">📊 控制台与日志</a>
    </div>
  </header>

  <div class="bottom-overlay" id="bottom-info">
    <div class="info-date">
      <span id="info-date-text">-</span>
      <span id="info-type-badge" class="info-badge">照片</span>
    </div>
    <div class="info-meta">
      <span id="info-filename">-</span>
      <span id="info-extra">-</span>
      <span id="info-counter" class="info-counter">-</span>
    </div>
    <div id="info-path-box" style="margin-top:4px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:0.75rem;color:#93c5fd;opacity:0.9;word-break:break-all;user-select:text;-webkit-user-select:text;">
      📁 <span id="info-filepath">-</span>
    </div>
  </div>

  <div class="video-progress-container" id="video-progress-container" style="display: none;">
    <div class="video-progress-bar" id="video-progress-bar"></div>
  </div>

  <div class="timeline-scrubber" id="timeline-scrubber">
    <div class="scrubber-track" id="scrubber-track">
      <div class="scrubber-handle" id="scrubber-handle" style="top: 0%;"></div>
    </div>
    <div class="scrubber-tooltip" id="scrubber-tooltip">2024-01-01</div>
  </div>

  <div class="floating-nav">
    <button class="fab-btn" onclick="goPrev()" title="上一张 (↑)">▲</button>
    <button class="fab-btn" onclick="goNext()" title="下一张 (↓)">▼</button>
  </div>

  <script>
    let totalItems = 0;
    let itemsCache = {};
    let dateGroups = [];
    let currentIndex = 0;
    let isMuted = true;
    let isZoomed = false;
    let isTransitioning = false;

    // Segment state
    let segmentsList = [];
    let currentSegIndex = -1;
    let currentSegId = null;

    // Zoom/Pan state for current image
    let currentScale = 1;
    let panX = 0, panY = 0;
    let isDragging = false, dragStartX = 0, dragStartY = 0;

    const layerPrev = document.getElementById('layer-prev');
    const layerCurr = document.getElementById('layer-curr');
    const layerNext = document.getElementById('layer-next');
    const boxPrev = document.getElementById('box-prev');
    const boxCurr = document.getElementById('box-curr');
    const boxNext = document.getElementById('box-next');

    const scrubberTrack = document.getElementById('scrubber-track');
    const scrubberHandle = document.getElementById('scrubber-handle');
    const scrubberTooltip = document.getElementById('scrubber-tooltip');

    const soundBtn = document.getElementById('sound-btn');
    const videoProgressContainer = document.getElementById('video-progress-container');
    const videoProgressBar = document.getElementById('video-progress-bar');
    const segmentSelect = document.getElementById('segment-select');
    const prevSegBtn = document.getElementById('prev-seg-btn');
    const nextSegBtn = document.getElementById('next-seg-btn');
    const ytSegLink = document.getElementById('yt-seg-link');

    async function init() {
      try {
        const segRes = await fetch('/api/segments');
        if (segRes.ok) {
          const segData = await segRes.json();
          if (segData.segments && segData.segments.length > 0) {
            segmentsList = segData.segments;
            renderSegmentOptions();
            const savedSeg = localStorage.getItem('tubetape_active_seg');
            const targetSeg = segmentsList.find(s => s.id === savedSeg) || segmentsList[segmentsList.length - 1];
            await selectSegment(targetSeg.id);
            return;
          }
        }
      } catch (e) {}

      // Fallback if no segments yet: global summary
      try {
        const res = await fetch('/api/media/summary');
        if (res.ok) {
          const data = await res.json();
          totalItems = data.total || 0;
          dateGroups = data.date_groups || [];
          if (totalItems === 0) {
            renderEmpty();
            setTimeout(init, 3000);
            return;
          }
          await prefetchRange(0, 10);
          showSlide(0);
        } else {
          renderEmpty();
          setTimeout(init, 3000);
        }
      } catch (e) {
        renderEmpty();
        setTimeout(init, 3000);
      }
    }

    function renderSegmentOptions() {
      segmentSelect.innerHTML = '';
      segmentsList.forEach((s) => {
        const opt = document.createElement('option');
        opt.value = s.id;
        const ytTag = s.youtube_video_id ? ' ✅' : '';
        opt.textContent = `${s.title} (${s.file_count}项 · ${formatSec(s.duration_seconds)})${ytTag}`;
        segmentSelect.appendChild(opt);
      });
    }

    async function selectSegment(segId) {
      currentSegId = segId;
      localStorage.setItem('tubetape_active_seg', segId);
      currentSegIndex = segmentsList.findIndex(s => s.id === segId);
      segmentSelect.value = segId;

      prevSegBtn.disabled = (currentSegIndex <= 0);
      nextSegBtn.disabled = (currentSegIndex >= segmentsList.length - 1);

      const curSeg = segmentsList[currentSegIndex];
      if (curSeg && curSeg.youtube_video_id) {
        ytSegLink.href = 'https://youtu.be/' + curSeg.youtube_video_id;
        ytSegLink.style.display = 'inline-flex';
      } else {
        ytSegLink.style.display = 'none';
      }

      boxCurr.innerHTML = '<div class="loading-spinner"></div>';
      try {
        const res = await fetch('/api/segment/items?id=' + encodeURIComponent(segId));
        if (res.ok) {
          const data = await res.json();
          itemsCache = {};
          (data.items || []).forEach((item, idx) => {
            item.index = idx;
            itemsCache[idx] = item;
          });
          totalItems = (data.items || []).length;
          currentIndex = 0;
          if (totalItems > 0) {
            showSlide(0);
          } else {
            boxCurr.innerHTML = '<div class="empty-state"><div class="empty-title">该分片暂无素材</div></div>';
          }
        }
      } catch (e) {
        boxCurr.innerHTML = '<div class="empty-state"><div class="empty-title">载入分片失败</div></div>';
      }
    }

    function onSegmentSelect(segId) {
      if (segId) selectSegment(segId);
    }

    function stepSegment(delta) {
      const targetIdx = currentSegIndex + delta;
      if (targetIdx >= 0 && targetIdx < segmentsList.length) {
        selectSegment(segmentsList[targetIdx].id);
      }
    }

    function renderEmpty() {
      boxCurr.innerHTML = `
        <div class="empty-state">
          <div class="loading-spinner"></div>
          <div class="empty-title">正在扫描媒体文件...</div>
          <div class="empty-desc">TubeTape 正在建立时间线媒体索引。您可以前往控制台查看实时扫描进度与构建状态。</div>
          <a href="/log" class="btn-header" style="padding: 10px 22px; font-size: 0.95rem; margin-top: 8px;">📊 前往运行控制台与日志</a>
        </div>
      `;
      document.getElementById('bottom-info').style.display = 'none';
      document.getElementById('timeline-scrubber').style.display = 'none';
    }

    async function prefetchRange(start, count) {
      if (currentSegId) return; // Segment mode loads all items upfront
      const needed = [];
      for (let i = start; i < start + count && i < totalItems; i++) {
        if (!itemsCache[i]) needed.push(i);
      }
      if (needed.length === 0) return;

      const offset = Math.max(0, start);
      const limit = Math.min(100, Math.max(count, 50));
      try {
        const res = await fetch(`/api/media/items?offset=${offset}&limit=${limit}`);
        if (res.ok) {
          const data = await res.json();
          (data.items || []).forEach(item => {
            itemsCache[item.index] = item;
          });
        }
      } catch (e) {}
    }

    function buildMediaElement(item, isActive) {
      if (!item) return '<div class="loading-spinner"></div>';
      if (item.type === 'video') {
        return `
          <video id="vid-${item.index}"
                 src="/api/media/stream?id=${item.id}"
                 playsinline loop
                 ${isMuted ? 'muted' : ''}
                 preload="${isActive ? 'auto' : 'metadata'}">
          </video>
        `;
      } else {
        return `
          <img id="img-${item.index}"
               src="/api/media/view?id=${item.id}"
               alt="${escapeHtml(item.name)}"
               draggable="false"
               loading="${isActive ? 'eager' : 'lazy'}" />
        `;
      }
    }

    async function showSlide(index) {
      if (index < 0 || index >= totalItems) return;
      currentIndex = index;

      prefetchRange(Math.max(0, index - 5), 15);
      resetZoom();

      const itemCurr = itemsCache[index];
      const itemPrev = index > 0 ? itemsCache[index - 1] : null;
      const itemNext = index < totalItems - 1 ? itemsCache[index + 1] : null;

      boxCurr.innerHTML = buildMediaElement(itemCurr, true);
      boxPrev.innerHTML = buildMediaElement(itemPrev, false);
      boxNext.innerHTML = buildMediaElement(itemNext, false);

      updateInfoOverlay(itemCurr);
      updateScrubberPosition();
      bindCurrEvents(itemCurr);
    }

    function bindCurrEvents(item) {
      if (!item) return;
      if (item.type === 'video') {
        videoProgressContainer.style.display = 'block';
        const vid = document.getElementById(`vid-${item.index}`);
        if (vid) {
          vid.muted = isMuted;
          vid.play().catch(() => {});
          vid.ontimeupdate = () => {
            if (vid.duration) {
              const pct = (vid.currentTime / vid.duration) * 100;
              videoProgressBar.style.width = pct + '%';
            }
          };
          vid.onclick = () => {
            if (vid.paused) vid.play();
            else vid.pause();
          };
        }
      } else {
        videoProgressContainer.style.display = 'none';
        const img = document.getElementById(`img-${item.index}`);
        if (img) {
          img.ondblclick = (e) => handleDblClickZoom(e, img);
          setupPanEvents(img);
        }
      }
    }

    function updateInfoOverlay(item) {
      if (!item) return;
      document.getElementById('bottom-info').style.display = 'flex';
      document.getElementById('timeline-scrubber').style.display = 'flex';

      let dStr = item.captured_at ? item.captured_at.replace('T', ' ').replace('Z', ' UTC') : '无拍摄时间';
      document.getElementById('info-date-text').textContent = dStr;
      document.getElementById('info-type-badge').textContent = item.type === 'video' ? '🎬 视频' : '📷 照片';
      document.getElementById('info-filename').textContent = item.name;

      let extra = [];
      if (item.resolution) extra.push(item.resolution);
      if (item.duration > 0) extra.push(formatSec(item.duration));
      if (item.size > 0) extra.push(formatSize(item.size));
      document.getElementById('info-extra').textContent = extra.join(' · ');
      document.getElementById('info-counter').textContent = `${currentIndex + 1} / ${totalItems}`;

      const pathElem = document.getElementById('info-filepath');
      if (pathElem) {
        pathElem.textContent = item.abs_path || item.path || '-';
      }
    }

    function updateScrubberPosition() {
      if (totalItems <= 1) return;
      const pct = (currentIndex / (totalItems - 1)) * 100;
      scrubberHandle.style.top = pct + '%';
    }

    function goNext() {
      if (isTransitioning) return;
      if (currentIndex >= totalItems - 1) {
        if (currentSegIndex >= 0 && currentSegIndex < segmentsList.length - 1) {
          stepSegment(1);
        }
        return;
      }
      isTransitioning = true;

      // Animate curr up to -100%, next up to 0%
      layerCurr.style.transform = 'translateY(-100%)';
      layerNext.style.transform = 'translateY(0)';

      setTimeout(() => {
        // Reset transforms without transition
        layerCurr.style.transition = 'none';
        layerNext.style.transition = 'none';
        layerPrev.style.transition = 'none';

        layerCurr.style.transform = 'translateY(0)';
        layerNext.style.transform = 'translateY(100%)';
        layerPrev.style.transform = 'translateY(-100%)';

        void layerCurr.offsetHeight; // force reflow

        layerCurr.style.transition = '';
        layerNext.style.transition = '';
        layerPrev.style.transition = '';

        showSlide(currentIndex + 1);
        isTransitioning = false;
      }, 330);
    }

    function goPrev() {
      if (isTransitioning) return;
      if (currentIndex <= 0) {
        if (currentSegIndex > 0) {
          stepSegment(-1);
        }
        return;
      }
      isTransitioning = true;

      // Animate curr down to 100%, prev down to 0%
      layerCurr.style.transform = 'translateY(100%)';
      layerPrev.style.transform = 'translateY(0)';

      setTimeout(() => {
        layerCurr.style.transition = 'none';
        layerPrev.style.transition = 'none';
        layerNext.style.transition = 'none';

        layerCurr.style.transform = 'translateY(0)';
        layerPrev.style.transform = 'translateY(-100%)';
        layerNext.style.transform = 'translateY(100%)';

        void layerCurr.offsetHeight;

        layerCurr.style.transition = '';
        layerPrev.style.transition = '';
        layerNext.style.transition = '';

        showSlide(currentIndex - 1);
        isTransitioning = false;
      }, 330);
    }

    // Touch swipe navigation
    let touchStartY = 0;
    let touchStartTime = 0;

    window.addEventListener('touchstart', (e) => {
      if (e.touches.length === 1 && !isZoomed) {
        touchStartY = e.touches[0].clientY;
        touchStartTime = Date.now();
      }
    }, { passive: true });

    window.addEventListener('touchend', (e) => {
      if (isZoomed || e.changedTouches.length !== 1) return;
      const deltaY = e.changedTouches[0].clientY - touchStartY;
      const duration = Date.now() - touchStartTime;

      if (Math.abs(deltaY) > 40 && duration < 500) {
        if (deltaY < 0) goNext();
        else goPrev();
      }
    }, { passive: true });

    // Wheel navigation (debounced)
    let wheelTimeout = null;
    window.addEventListener('wheel', (e) => {
      if (isZoomed) return;
      if (wheelTimeout) return;
      wheelTimeout = setTimeout(() => { wheelTimeout = null; }, 260);

      if (e.deltaY > 20) goNext();
      else if (e.deltaY < -20) goPrev();
    }, { passive: true });

    // Keyboard navigation
    window.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'PageDown') {
        e.preventDefault();
        goNext();
      } else if (e.key === 'ArrowUp' || e.key === 'PageUp') {
        e.preventDefault();
        goPrev();
      } else if (e.key === ' ') {
        e.preventDefault();
        const vid = document.getElementById(`vid-${currentIndex}`);
        if (vid) {
          if (vid.paused) vid.play();
          else vid.pause();
        } else {
          goNext();
        }
      }
    });

    // Zoom and pan logic
    function handleDblClickZoom(e, img) {
      if (currentScale > 1) {
        resetZoom();
      } else {
        currentScale = 2.5;
        isZoomed = true;
        img.classList.add('zoomed');
        const rect = img.getBoundingClientRect();
        panX = (rect.width / 2 - (e.clientX - rect.left)) * 1.5;
        panY = (rect.height / 2 - (e.clientY - rect.top)) * 1.5;
        applyTransform(img);
        document.getElementById('zoom-btn').textContent = '🔍 还原';
      }
    }

    function toggleZoomCurr() {
      const img = document.getElementById(`img-${currentIndex}`);
      if (!img) return;
      if (currentScale > 1) {
        resetZoom();
      } else {
        currentScale = 2.5;
        isZoomed = true;
        img.classList.add('zoomed');
        panX = 0; panY = 0;
        applyTransform(img);
        document.getElementById('zoom-btn').textContent = '🔍 还原';
      }
    }

    function resetZoom() {
      currentScale = 1;
      panX = 0; panY = 0;
      isZoomed = false;
      const img = document.getElementById(`img-${currentIndex}`);
      if (img) {
        img.classList.remove('zoomed', 'dragging');
        img.style.transform = '';
      }
      document.getElementById('zoom-btn').textContent = '🔍 放大';
    }

    function applyTransform(img) {
      img.style.transform = `scale(${currentScale}) translate(${panX / currentScale}px, ${panY / currentScale}px)`;
    }

    function setupPanEvents(img) {
      img.onmousedown = (e) => {
        if (currentScale <= 1) return;
        isDragging = true;
        dragStartX = e.clientX - panX;
        dragStartY = e.clientY - panY;
        img.classList.add('dragging');
      };
      window.onmousemove = (e) => {
        if (!isDragging) return;
        panX = e.clientX - dragStartX;
        panY = e.clientY - dragStartY;
        applyTransform(img);
      };
      window.onmouseup = () => {
        if (isDragging) {
          isDragging = false;
          img.classList.remove('dragging');
        }
      };
    }

    function toggleMute() {
      isMuted = !isMuted;
      soundBtn.textContent = isMuted ? '🔇 静音' : '🔊 声音';
      const vid = document.getElementById(`vid-${currentIndex}`);
      if (vid) vid.muted = isMuted;
    }

    // Timeline Scrubber Dragging
    let isScrubbing = false;

    function handleScrubberMove(clientY) {
      const rect = scrubberTrack.getBoundingClientRect();
      let pos = (clientY - rect.top) / rect.height;
      pos = Math.max(0, Math.min(1, pos));
      const targetIdx = Math.round(pos * (totalItems - 1));

      scrubberHandle.style.top = (pos * 100) + '%';
      scrubberTooltip.style.top = (pos * 100) + '%';
      scrubberTooltip.style.display = 'block';

      const item = itemsCache[targetIdx];
      const d = item && item.captured_at ? item.captured_at.slice(0, 10) : `第 ${targetIdx + 1} 个`;
      scrubberTooltip.textContent = `${d} (${targetIdx + 1}/${totalItems})`;

      return targetIdx;
    }

    scrubberTrack.addEventListener('mousedown', (e) => {
      isScrubbing = true;
      const idx = handleScrubberMove(e.clientY);
      showSlide(idx);
    });

    window.addEventListener('mousemove', (e) => {
      if (!isScrubbing) return;
      const idx = handleScrubberMove(e.clientY);
      showSlide(idx);
    });

    window.addEventListener('mouseup', () => {
      if (isScrubbing) {
        isScrubbing = false;
        scrubberTooltip.style.display = 'none';
      }
    });

    scrubberTrack.addEventListener('touchstart', (e) => {
      if (e.touches.length === 1) {
        isScrubbing = true;
        const idx = handleScrubberMove(e.touches[0].clientY);
        showSlide(idx);
      }
    }, { passive: true });

    window.addEventListener('touchmove', (e) => {
      if (isScrubbing && e.touches.length === 1) {
        const idx = handleScrubberMove(e.touches[0].clientY);
        showSlide(idx);
      }
    }, { passive: true });

    window.addEventListener('touchend', () => {
      if (isScrubbing) {
        isScrubbing = false;
        scrubberTooltip.style.display = 'none';
      }
    });

    // Helpers
    function escapeHtml(text) {
      return (text || '').replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }
    function formatSec(sec) {
      const s = Math.round(sec);
      const m = Math.floor(s / 60);
      return `${m}:${(s % 60).toString().padStart(2, '0')}`;
    }
    function formatSize(bytes) {
      if (bytes >= 1024 * 1024 * 1024) return (bytes / (1024 * 1024 * 1024)).toFixed(1) + ' GB';
      if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
      return Math.round(bytes / 1024) + ' KB';
    }

    init();
  </script>
</body>
</html>
"""

_LOG_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TubeTape 运行控制台与仪表盘</title>
  <style>
    :root {
      --bg: #121214;
      --card: #1c1c1f;
      --card-alt: #161619;
      --border: #2e2e33;
      --text: #e1e1e6;
      --muted: #8b8b94;
      --accent: #e50914;
      --success: #10b981;
      --blue: #3b82f6;
      --warning: #f59e0b;
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
      padding: 10px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-shrink: 0;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      font-weight: 700;
      font-size: 1.15rem;
    }
    .brand span {
      background: var(--accent);
      color: #fff;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 0.82rem;
      letter-spacing: 0.5px;
    }
    .header-right {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .btn-gallery {
      background: #2563eb;
      color: #fff;
      padding: 6px 14px;
      border-radius: 6px;
      text-decoration: none;
      font-size: 0.85rem;
      font-weight: 600;
      transition: background 0.15s;
    }
    .btn-gallery:hover { background: #1d4ed8; }
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
      0% { opacity: 0.4; } 50% { opacity: 1; } 100% { opacity: 0.4; }
    }
    main {
      flex: 1;
      padding: 16px 20px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    /* OAuth Banner */
    .auth-banner {
      background: #181b24;
      border: 1px solid var(--blue);
      border-radius: 8px;
      padding: 16px 20px;
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
    .auth-badge {
      background: rgba(245, 158, 11, 0.2);
      border: 1px solid var(--warning);
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
      border: 1px solid var(--border);
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
      color: var(--muted);
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
    .auth-btn-submit {
      background: #10b981;
      color: #fff;
      border: 1px solid #10b981;
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 0.82rem;
      font-weight: 600;
      cursor: pointer;
    }
    .auth-feedback { font-size: 0.8rem; min-height: 1.2rem; }
    .fb-error { color: #f87171; font-weight: 600; }
    .fb-success { color: #34d399; font-weight: 600; }
    .fb-info { color: #93c5fd; }

    /* Dashboard Metrics Cards */
    .metrics-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 14px;
      flex-shrink: 0;
    }
    .metric-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 16px;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .metric-label {
      font-size: 0.82rem;
      color: var(--muted);
      font-weight: 500;
    }
    .metric-value {
      font-size: 1.55rem;
      font-weight: 700;
      color: #fff;
      display: flex;
      align-items: baseline;
      gap: 6px;
    }
    .metric-sub {
      font-size: 0.78rem;
      color: #a1a1aa;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    /* Segments Section */
    .section-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      flex-shrink: 0;
    }
    .section-header {
      padding: 12px 18px;
      background: var(--card-alt);
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .section-title {
      font-size: 0.95rem;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .table-container {
      overflow-x: auto;
      max-height: 280px;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      text-align: left;
    }
    th {
      background: #141417;
      color: var(--muted);
      padding: 10px 16px;
      font-weight: 600;
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
    }
    td {
      padding: 10px 16px;
      border-bottom: 1px solid #232328;
      vertical-align: middle;
      white-space: nowrap;
    }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: rgba(255, 255, 255, 0.02); }

    .badge {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .badge-uploaded { background: rgba(16, 185, 129, 0.15); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.3); }
    .badge-building { background: rgba(59, 130, 246, 0.15); color: #93c5fd; border: 1px solid rgba(59, 130, 246, 0.3); }
    .badge-pending { background: rgba(161, 161, 170, 0.15); color: #d4d4d8; border: 1px solid rgba(161, 161, 170, 0.3); }
    .badge-failed { background: rgba(239, 68, 68, 0.15); color: #fca5a5; border: 1px solid rgba(239, 68, 68, 0.3); }

    .yt-link {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      color: #93c5fd;
      text-decoration: none;
      font-weight: 600;
    }
    .yt-link:hover { text-decoration: underline; }

    .progress-bar-wrap {
      width: 140px;
      background: #27272a;
      height: 6px;
      border-radius: 3px;
      overflow: hidden;
      margin-top: 4px;
    }
    .progress-bar-fill {
      height: 100%;
      background: var(--blue);
      border-radius: 3px;
      transition: width 0.3s;
    }

    /* Log Stream Section */
    .log-section {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      display: flex;
      flex-direction: column;
      flex: 1;
      min-height: 260px;
      overflow: hidden;
    }
    .log-header {
      padding: 10px 18px;
      background: var(--card-alt);
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .log-controls {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    select, button {
      background: #2a2a30;
      color: var(--text);
      border: 1px solid var(--border);
      padding: 5px 10px;
      border-radius: 5px;
      font-size: 0.82rem;
      cursor: pointer;
    }
    button.active {
      background: rgba(16, 185, 129, 0.2);
      border-color: var(--success);
      color: #6ee7b7;
    }
    .log-box {
      flex: 1;
      background: #0d0d10;
      padding: 12px 16px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.83rem;
      line-height: 1.5;
      color: #d4d4d8;
      overflow-y: auto;
      white-space: pre-wrap;
      word-break: break-all;
    }
    .log-line-info { color: #93c5fd; }
    .log-line-warn { color: #fcd34d; font-weight: 600; }
    .log-line-error { color: #f87171; font-weight: 700; }
    .log-line-debug { color: #71717a; }

    .btn-top {
      background: #27272e;
      border: 1px solid var(--border);
      color: #fff;
      padding: 6px 13px;
      border-radius: 6px;
      font-size: 0.83rem;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 6px;
      cursor: pointer;
      transition: background 0.15s;
    }
    .btn-top:hover { background: #353540; }
    .btn-scan {
      background: rgba(16, 185, 129, 0.15);
      border: 1px solid rgba(16, 185, 129, 0.4);
      color: #34d399;
    }
    .btn-scan:hover { background: rgba(16, 185, 129, 0.28); }
    .btn-action {
      padding: 3px 8px;
      border-radius: 4px;
      font-size: 0.76rem;
      font-weight: 500;
      border: 1px solid var(--border);
      background: #27272e;
      color: #e1e1e6;
      cursor: pointer;
      transition: all 0.15s;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .btn-action:hover:not(:disabled) {
      background: #363640;
      border-color: #555;
    }
    .btn-action:disabled {
      opacity: 0.35;
      cursor: not-allowed;
    }
    .btn-action-rebuild { color: #93c5fd; }
    .btn-action-upload { color: #86efac; }
    .btn-action-delete { color: #fca5a5; }
    .btn-action-delete:hover:not(:disabled) { background: rgba(239, 68, 68, 0.2); border-color: #ef4444; }

    /* Modal */
    .modal-overlay {
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0, 0, 0, 0.75);
      backdrop-filter: blur(4px);
      z-index: 1000;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 20px;
    }
    .modal-card {
      background: #18181c;
      border: 1px solid var(--border);
      border-radius: 10px;
      max-width: 680px;
      width: 100%;
      max-height: 85vh;
      display: flex;
      flex-direction: column;
      box-shadow: 0 10px 40px rgba(0, 0, 0, 0.6);
    }
    .modal-header {
      padding: 14px 20px;
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .modal-header h3 { font-size: 1.05rem; font-weight: 700; color: #fff; }
    .modal-body {
      padding: 16px 20px;
      overflow-y: auto;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }
    .modal-footer {
      padding: 12px 20px;
      border-top: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 12px;
      background: #141417;
      border-radius: 0 0 10px 10px;
    }
    .form-group-title {
      font-size: 0.88rem;
      font-weight: 700;
      color: #93c5fd;
      margin-bottom: 8px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .form-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
    }
    .form-item {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .form-label {
      font-size: 0.78rem;
      color: #a1a1aa;
      font-weight: 500;
      display: flex;
      align-items: center;
      gap: 4px;
    }
    .form-label .tag-warn {
      color: #f59e0b;
      font-size: 0.7rem;
    }
    .form-control {
      background: #0f0f12;
      border: 1px solid #33333a;
      color: #fff;
      padding: 6px 10px;
      border-radius: 6px;
      font-size: 0.83rem;
      outline: none;
    }
    .form-control:focus { border-color: #3b82f6; }
    .warn-box {
      background: rgba(245, 158, 11, 0.1);
      border: 1px solid rgba(245, 158, 11, 0.4);
      color: #fcd34d;
      padding: 10px 14px;
      border-radius: 6px;
      font-size: 0.82rem;
      line-height: 1.45;
      display: none;
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <span>TubeTape</span>
      <div>运行控制台与仪表盘</div>
    </div>
    <div class="header-right">
      <button id="scan-btn" class="btn-top btn-scan" onclick="startScan()">🔄 开始全量扫描</button>
      <button class="btn-top" onclick="openConfigModal()">⚙️ 参数配置</button>
      <a href="/" class="btn-gallery">📱 打开全屏画廊 (/)</a>
      <div class="status-badge">
        <div class="status-dot"></div>
        <div id="status-text">初始化中...</div>
      </div>
    </div>
  </header>

  <main>
    <!-- OAuth Banner -->
    <div id="auth-banner" class="auth-banner" style="display: none;">
      <div class="auth-banner-header">
        <div class="auth-banner-title">
          <span>🔑</span>
          <strong>需要完成 Google YouTube 授权</strong>
        </div>
        <div class="auth-badge">等待授权中</div>
      </div>
      <div class="auth-banner-desc">
        TubeTape 尚未获得 YouTube 访问凭据（<code>token.json</code> 不存在或已失效）。请通过以下任一方式完成授权：
      </div>
      <div class="auth-methods">
        <div class="auth-method-card">
          <div>
            <div class="auth-method-title">方式一：一键直接授权（本机或端口映射环境）</div>
            <div class="auth-method-desc">点击下方按钮前往 Google 授权。若本机可直接访问 <code>http://localhost:8080</code>，授权后将自动回调保存凭据。</div>
          </div>
          <a id="auth-link-btn" href="#" target="_blank" class="auth-btn-primary">🔗 点击前往 Google 账号授权</a>
        </div>
        <div class="auth-method-card">
          <div>
            <div class="auth-method-title">方式二：手动粘贴地址栏 URL（远程 NAS / 无桌面服务器）</div>
            <div class="auth-method-desc">若在局域网 NAS 上运行，点击上方授权后，直接将浏览器地址栏中的完整 URL 粘贴到下方即可：</div>
          </div>
          <div class="auth-input-row">
            <input type="text" id="auth-url-input" placeholder="粘贴浏览器地址栏完整 URL 或 code..." />
            <button id="auth-submit-btn" class="auth-btn-submit" onclick="submitAuthCode()">提交凭据</button>
          </div>
          <div id="auth-feedback" class="auth-feedback"></div>
        </div>
      </div>
    </div>

    <!-- Metrics Cards -->
    <div class="metrics-grid">
      <div class="metric-card">
        <div class="metric-label">扫描进度 / 媒体总数</div>
        <div class="metric-value">
          <span id="metric-files">0</span>
          <span style="font-size: 0.85rem; font-weight: normal; color: #a1a1aa;">个文件</span>
        </div>
        <div class="metric-sub" id="metric-scan-detail">正在检查...</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">时间线分段 (Segments)</div>
        <div class="metric-value">
          <span id="metric-segments">0</span>
          <span style="font-size: 0.85rem; font-weight: normal; color: #a1a1aa;">个分段</span>
        </div>
        <div class="metric-sub" id="metric-segments-sub">已上传: 0 | 待构建: 0</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">本地磁盘保留视频</div>
        <div class="metric-value">
          <span id="metric-disk-segments">0</span>
          <span style="font-size: 0.85rem; font-weight: normal; color: #a1a1aa;">部 (限制: <span id="metric-keep-limit">0</span>)</span>
        </div>
        <div class="metric-sub">目录: uploaded_segments/</div>
      </div>
      <div class="metric-card">
        <div class="metric-label">当前运行任务</div>
        <div class="metric-value" style="font-size: 1.15rem;" id="metric-task">
          空闲
        </div>
        <div class="metric-sub" id="metric-task-sub">就绪</div>
      </div>
    </div>

    <!-- Timeline Segments Section -->
    <div class="section-card">
      <div class="section-header">
        <div class="section-title">
          <span>🎞️</span>
          <span>时间线分段列表 (Timeline Segments)</span>
        </div>
        <div style="font-size: 0.8rem; color: var(--muted);" id="segments-count-text">共 0 个分段</div>
      </div>
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>时间区间 / 分段标题</th>
              <th>媒体数</th>
              <th>时长</th>
              <th>构建状态 / 进度</th>
              <th>YouTube 视频</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody id="segments-table-body">
            <tr><td colspan="6" style="text-align: center; color: #888;">暂无分段信息</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Live Log Stream Section -->
    <div class="log-section">
      <div class="log-header">
        <div class="section-title">
          <span>📜</span>
          <span>实时运行日志 (Live Audit Log)</span>
        </div>
        <div class="log-controls">
          <label style="font-size: 0.82rem; color: var(--muted);">显示最近:</label>
          <select id="log-limit-select" onchange="changeLogLimit(this.value)">
            <option value="50">50 条</option>
            <option value="100" selected>100 条 (默认)</option>
            <option value="200">200 条</option>
            <option value="500">500 条</option>
            <option value="0">全部</option>
          </select>
          <button id="autoscroll-btn" class="active" onclick="toggleAutoScroll()">自动滚动: 开</button>
          <button onclick="clearConsole()">清屏</button>
        </div>
      </div>
      <div id="log-box" class="log-box">正在连接日志流...\n</div>
    </div>
  </main>

  <!-- Config Modal -->
  <div id="config-modal" class="modal-overlay">
    <div class="modal-card">
      <div class="modal-header">
        <h3>⚙️ 系统运行参数配置</h3>
        <button class="btn-action" style="padding: 4px 10px;" onclick="closeConfigModal()">✕ 关闭</button>
      </div>
      <div class="modal-body">
        <div id="config-warn-box" class="warn-box">
          ⚠️ <strong>高危警示：检测到修改了影响分片指纹（Segment ID）的核心参数！</strong><br>
          修改这些参数会导致后续扫描规划时，所有已有分片的指纹不匹配，系统会认为视频规格已过时，从而可能触发历史视频的全部重新转码与重新上传（替换原视频）。请谨慎修改！
        </div>

        <div>
          <div class="form-group-title"><span>🎞️</span> 核心分段与转码参数 <span class="tag-warn" style="font-size:0.75rem;">(⚠️ 影响 Segment ID 指纹)</span></div>
          <div class="form-grid">
            <div class="form-item">
              <label class="form-label">分段最大时长 (例如 20m, 1h, 1200):</label>
              <input class="form-control" id="cfg-segment_duration" type="text" oninput="checkConfigChanges()" />
            </div>
            <div class="form-item">
              <label class="form-label">照片播放时长 (秒):</label>
              <input class="form-control" id="cfg-image_duration" type="number" step="0.5" min="0.5" oninput="checkConfigChanges()" />
            </div>
            <div class="form-item">
              <label class="form-label">CRF 转码画质 (0-51, 越小画质越高):</label>
              <input class="form-control" id="cfg-crf" type="number" min="0" max="51" oninput="checkConfigChanges()" />
            </div>
            <div class="form-item">
              <label class="form-label">最大分辨率 (如 7680x4320, 1920x1080):</label>
              <input class="form-control" id="cfg-max_resolution" type="text" oninput="checkConfigChanges()" />
            </div>
            <div class="form-item">
              <label class="form-label">输出帧率 FPS (如 30, 60):</label>
              <input class="form-control" id="cfg-fps" type="number" min="1" max="120" oninput="checkConfigChanges()" />
            </div>
            <div class="form-item">
              <label class="form-label">画布模式 (Canvas Mode):</label>
              <select class="form-control" id="cfg-canvas_mode" onchange="checkConfigChanges()">
                <option value="max">max (包围盒不缩放)</option>
                <option value="first">first (首文件分辨率)</option>
              </select>
            </div>
            <div class="form-item">
              <label class="form-label">x264 编码预设 (Preset):</label>
              <select class="form-control" id="cfg-x264_preset" onchange="checkConfigChanges()">
                <option value="ultrafast">ultrafast</option>
                <option value="superfast">superfast</option>
                <option value="veryfast">veryfast</option>
                <option value="faster">faster</option>
                <option value="fast">fast</option>
                <option value="medium">medium</option>
                <option value="slow">slow</option>
                <option value="slower">slower</option>
                <option value="veryslow">veryslow</option>
              </select>
            </div>
            <div class="form-item" style="justify-content: center;">
              <label class="form-label">
                <input type="checkbox" id="cfg-ken_burns" onchange="checkConfigChanges()" style="margin-right: 6px;" />
                启用 Ken Burns (照片平移缩放运镜)
              </label>
            </div>
          </div>
        </div>

        <div>
          <div class="form-group-title"><span>☁️</span> 上传与本地保留策略</div>
          <div class="form-grid">
            <div class="form-item">
              <label class="form-label">本地磁盘保留视频数量 (0 为构建上传后即删):</label>
              <input class="form-control" id="cfg-keep_segments" type="number" min="0" />
            </div>
            <div class="form-item" style="justify-content: center;">
              <label class="form-label">
                <input type="checkbox" id="cfg-no_upload" style="margin-right: 6px;" />
                仅本地构建，不上传到 YouTube (--no-upload)
              </label>
            </div>
            <div class="form-item" style="justify-content: center;">
              <label class="form-label">
                <input type="checkbox" id="cfg-no_scan" style="margin-right: 6px;" />
                启动时跳过扫描，直接读取数据库 (--no-scan)
              </label>
            </div>
            <div class="form-item">
              <label class="form-label">YouTube 隐私设置:</label>
              <select class="form-control" id="cfg-privacy">
                <option value="private">private (私享)</option>
                <option value="unlisted">unlisted (不公开列出)</option>
              </select>
            </div>
            <div class="form-item" style="grid-column: 1 / -1;">
              <label class="form-label">YouTube 播放列表 ID (可选):</label>
              <input class="form-control" id="cfg-playlist" type="text" placeholder="留空则不加入播放列表" />
            </div>
          </div>
        </div>

        <div>
          <div class="form-group-title"><span>🔍</span> 文件过滤与 Watch 监听设置</div>
          <div class="form-grid">
            <div class="form-item">
              <label class="form-label">Watch 静默期 (如 10m):</label>
              <input class="form-control" id="cfg-quiet_period" type="text" />
            </div>
            <div class="form-item">
              <label class="form-label">Watch 轮询周期 (如 30s):</label>
              <input class="form-control" id="cfg-poll_interval" type="text" />
            </div>
            <div class="form-item" style="justify-content: center;">
              <label class="form-label">
                <input type="checkbox" id="cfg-only_camera_photos" style="margin-right: 6px;" />
                仅处理相机照片 (过滤截图/下载)
              </label>
            </div>
            <div class="form-item" style="justify-content: center;">
              <label class="form-label">
                <input type="checkbox" id="cfg-only_phone_videos" style="margin-right: 6px;" />
                仅处理手机拍摄视频
              </label>
            </div>
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn-action" style="padding: 6px 16px;" onclick="closeConfigModal()">取消</button>
        <button class="auth-btn-primary" style="padding: 6px 18px; border: none; cursor: pointer;" onclick="saveConfig()">保存并应用配置</button>
      </div>
    </div>
  </div>

  <script>
    let autoScroll = true;
    let offset = 0;
    let isInitial = true;
    let currentAuthUrl = '';
    let maxLines = 100;
    let logLines = [];

    const logBox = document.getElementById('log-box');
    const statusText = document.getElementById('status-text');

    function toggleAutoScroll() {
      autoScroll = !autoScroll;
      const btn = document.getElementById('autoscroll-btn');
      btn.textContent = '自动滚动: ' + (autoScroll ? '开' : '关');
      btn.className = autoScroll ? 'active' : '';
    }

    function clearConsole() {
      logLines = [];
      logBox.textContent = '';
    }

    function changeLogLimit(val) {
      maxLines = parseInt(val, 10);
      renderLogBox();
    }

    function formatLine(line) {
      if (line.includes(' ERROR ')) return `<span class="log-line-error">${escapeHtml(line)}</span>`;
      if (line.includes(' WARNING ')) return `<span class="log-line-warn">${escapeHtml(line)}</span>`;
      if (line.includes(' INFO ')) return `<span class="log-line-info">${escapeHtml(line)}</span>`;
      if (line.includes(' DEBUG ')) return `<span class="log-line-debug">${escapeHtml(line)}</span>`;
      return escapeHtml(line);
    }

    function renderLogBox() {
      let linesToRender = logLines;
      if (maxLines > 0 && logLines.length > maxLines) {
        linesToRender = logLines.slice(logLines.length - maxLines);
      }
      logBox.innerHTML = linesToRender.map(l => formatLine(l)).join('\\n') + '\\n';
      if (autoScroll) {
        logBox.scrollTop = logBox.scrollHeight;
      }
    }

    function escapeHtml(text) {
      return (text || '').replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
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
        btn.textContent = '提交凭据';
      }
    }

    async function pollDashboard() {
      try {
        const res = await fetch('/api/dashboard');
        if (res.ok) {
          const d = await res.json();
          let displayStatus = d.task || d.status || '就绪';
          if (d.status === 'scanning' && !d.task) displayStatus = '正在扫描...';
          else if (d.status === 'idle' && !d.task) displayStatus = '空闲就绪';
          else if (d.status === 'watching') displayStatus = '常驻监听中';
          statusText.textContent = displayStatus;

          const dot = document.querySelector('.status-dot');
          if (dot) {
            if (d.status === 'scanning' || d.status === 'transcoding' || d.status === 'building') {
              dot.style.background = '#f59e0b';
              dot.style.boxShadow = '0 0 8px #f59e0b';
            } else if (d.status === 'error' || d.status === 'failed') {
              dot.style.background = '#ef4444';
              dot.style.boxShadow = '0 0 8px #ef4444';
            } else {
              dot.style.background = '#10b981';
              dot.style.boxShadow = '0 0 8px #10b981';
            }
          }

          // OAuth banner
          const authBanner = document.getElementById('auth-banner');
          if (d.auth_required && d.auth_url) {
            authBanner.style.display = 'block';
            if (currentAuthUrl !== d.auth_url) {
              currentAuthUrl = d.auth_url;
              document.getElementById('auth-link-btn').href = d.auth_url;
            }
          } else {
            authBanner.style.display = 'none';
          }

          // Metrics
          document.getElementById('metric-files').textContent = (d.stats && d.stats.total_files ? d.stats.total_files : 0).toLocaleString();
          if (d.scanner && d.scanner.is_scanning) {
            document.getElementById('metric-scan-detail').innerHTML = `🔍 扫描中: ${d.scanner.count} 个 (${escapeHtml(d.scanner.current || '')})`;
          } else {
            document.getElementById('metric-scan-detail').textContent = '✅ 扫描就绪';
          }

          document.getElementById('metric-segments').textContent = d.stats ? d.stats.total_segments : 0;
          document.getElementById('metric-segments-sub').textContent =
            d.stats ? `已上传: ${d.stats.uploaded_segments} | 待处理: ${d.stats.total_segments - d.stats.uploaded_segments}` : '-';

          document.getElementById('metric-disk-segments').textContent = d.stats ? d.stats.disk_segments_count : 0;
          document.getElementById('metric-keep-limit').textContent = d.stats ? d.stats.keep_segments : 0;

          document.getElementById('metric-task').textContent = d.status || '空闲';
          document.getElementById('metric-task-sub').textContent = d.task || '-';

          // Segments Table
          const tbody = document.getElementById('segments-table-body');
          document.getElementById('segments-count-text').textContent = `共 ${d.segments ? d.segments.length : 0} 个分段`;
          if (!d.segments || d.segments.length === 0) {
            tbody.innerHTML = '<tr><td colspan="6" style="text-align: center; color: #888;">暂无分段信息</td></tr>';
          } else {
            tbody.innerHTML = d.segments.map(seg => {
              let statusHtml = '';
              if (seg.status === 'uploaded') {
                statusHtml = '<span class="badge badge-uploaded">✅ 已上传</span>';
              } else if (seg.status === 'building') {
                const prog = seg.progress || { done: 0, total: 1, current_file: '' };
                const pct = prog.total > 0 ? Math.round((prog.done / prog.total) * 100) : 0;
                statusHtml = `
                  <div>
                    <span class="badge badge-building">⚡ 正在构建 (${prog.done}/${prog.total})</span>
                    <div class="progress-bar-wrap"><div class="progress-bar-fill" style="width: ${pct}%"></div></div>
                    <div style="font-size: 0.72rem; color: #888; max-width: 180px; overflow: hidden; text-overflow: ellipsis;">${escapeHtml(prog.current_file || '')}</div>
                  </div>
                `;
              } else if (seg.status === 'pending') {
                statusHtml = '<span class="badge badge-pending">⏳ 等待构建</span>';
              } else if (seg.status === 'failed') {
                statusHtml = '<span class="badge badge-failed">❌ 构建失败</span>';
              } else if (seg.status === 'sealed' && !seg.youtube_video_id) {
                statusHtml = '<span class="badge badge-uploaded" style="color: #60a5fa; border-color: rgba(96,165,250,0.3); background: rgba(96,165,250,0.15);">📦 本地就绪</span>';
              } else {
                statusHtml = `<span class="badge badge-pending">${escapeHtml(seg.status)}</span>`;
              }

              let ytHtml = '-';
              if (seg.youtube_video_id) {
                ytHtml = `<a href="https://youtu.be/${seg.youtube_video_id}" target="_blank" class="yt-link">▶️ 查看视频 (${seg.youtube_video_id})</a>`;
              }

              let actionsHtml = `
                <div style="display: flex; gap: 6px; align-items: center;">
                  <button class="btn-action btn-action-rebuild" onclick="rebuildSegment('${seg.segment_id}')" title="强制重新转码构建该分片">🔨 重建</button>
                  <button class="btn-action btn-action-upload" ${seg.has_local_file ? '' : 'disabled title="本地 uploaded_segments/ 中未找到视频文件，需先重新构建"'} onclick="uploadSegment('${seg.segment_id}')">☁️ 上传</button>
                  <button class="btn-action btn-action-delete" ${seg.youtube_video_id ? '' : 'disabled title="未上传到 YouTube"'} onclick="deleteYoutubeVideo('${seg.segment_id}', '${seg.youtube_video_id || ''}')">🗑️ 删视频</button>
                </div>
              `;

              return `
                <tr>
                  <td><strong>${escapeHtml(seg.title)}</strong></td>
                  <td>${seg.file_count} 张/条</td>
                  <td>${seg.duration_text}</td>
                  <td>${statusHtml}</td>
                  <td>${ytHtml}</td>
                  <td>${actionsHtml}</td>
                </tr>
              `;
            }).join('');
          }

        }
      } catch (e) {}
    }

    async function pollLogs() {
      try {
        const url = `/api/logs?offset=${offset}&initial=${isInitial ? 1 : 0}`;
        const res = await fetch(url);
        if (res.ok) {
          const data = await res.json();
          offset = data.offset;
          if (isInitial) {
            logLines = [];
            isInitial = false;
          }
          if (data.content) {
            const lines = data.content.split('\\n');
            for (let i = 0; i < lines.length; i++) {
              if (lines[i] || i < lines.length - 1) {
                logLines.push(lines[i]);
              }
            }
            renderLogBox();
          } else if (logLines.length === 0) {
            logBox.textContent = '暂无新日志记录 (等待写入...)\\n';
          }
        }
      } catch (e) {}
      setTimeout(pollLogs, 1500);
    }

    let initialConfig = null;
    let fingerprintParams = [];

    async function openConfigModal() {
      try {
        const res = await fetch('/api/config');
        if (!res.ok) throw new Error('无法读取配置');
        const data = await res.json();
        initialConfig = data.config || {};
        fingerprintParams = data.fingerprint_params || [];

        document.getElementById('cfg-segment_duration').value = initialConfig.segment_duration + 's';
        document.getElementById('cfg-image_duration').value = initialConfig.image_duration;
        document.getElementById('cfg-crf').value = initialConfig.crf;
        document.getElementById('cfg-max_resolution').value = initialConfig.max_resolution;
        document.getElementById('cfg-fps').value = initialConfig.fps;
        document.getElementById('cfg-canvas_mode').value = initialConfig.canvas_mode;
        document.getElementById('cfg-x264_preset').value = initialConfig.x264_preset;
        document.getElementById('cfg-ken_burns').checked = !!initialConfig.ken_burns;

        document.getElementById('cfg-keep_segments').value = initialConfig.keep_segments;
        document.getElementById('cfg-no_upload').checked = !!initialConfig.no_upload;
        document.getElementById('cfg-no_scan').checked = !!initialConfig.no_scan;
        document.getElementById('cfg-privacy').value = initialConfig.privacy || 'private';
        document.getElementById('cfg-playlist').value = initialConfig.playlist || '';

        document.getElementById('cfg-quiet_period').value = initialConfig.quiet_period + 's';
        document.getElementById('cfg-poll_interval').value = initialConfig.poll_interval + 's';
        document.getElementById('cfg-only_camera_photos').checked = !!initialConfig.only_camera_photos;
        document.getElementById('cfg-only_phone_videos').checked = !!initialConfig.only_phone_videos;

        checkConfigChanges();
        document.getElementById('config-modal').style.display = 'flex';
      } catch (err) {
        alert('打开配置失败: ' + err.message);
      }
    }

    function closeConfigModal() {
      document.getElementById('config-modal').style.display = 'none';
    }

    function checkConfigChanges() {
      if (!initialConfig) return false;
      let changed = false;
      const currentFp = {
        image_duration: parseFloat(document.getElementById('cfg-image_duration').value),
        crf: parseInt(document.getElementById('cfg-crf').value, 10),
        max_resolution: document.getElementById('cfg-max_resolution').value.trim(),
        fps: parseInt(document.getElementById('cfg-fps').value, 10),
        canvas_mode: document.getElementById('cfg-canvas_mode').value,
        x264_preset: document.getElementById('cfg-x264_preset').value,
        ken_burns: document.getElementById('cfg-ken_burns').checked,
      };

      if (currentFp.image_duration !== initialConfig.image_duration ||
          currentFp.crf !== initialConfig.crf ||
          currentFp.max_resolution !== initialConfig.max_resolution ||
          currentFp.fps !== initialConfig.fps ||
          currentFp.canvas_mode !== initialConfig.canvas_mode ||
          currentFp.x264_preset !== initialConfig.x264_preset ||
          currentFp.ken_burns !== initialConfig.ken_burns) {
        changed = true;
      }
      const warnBox = document.getElementById('config-warn-box');
      if (warnBox) warnBox.style.display = changed ? 'block' : 'none';
      return changed;
    }

    async function saveConfig() {
      const hasFpChanges = checkConfigChanges();
      if (hasFpChanges) {
        const ok = confirm(
          "⚠️ 高危警告：检测到您修改了影响 Segment ID（分片指纹）的核心转码参数！\\n\\n" +
          "修改这些参数会导致所有既有分片指纹与新参数不符，后续可能触发所有历史视频重新转码并重新上传到 YouTube。\\n\\n" +
          "是否确认保存并应用新配置？"
        );
        if (!ok) return;
      }

      const payload = {
        segment_duration: document.getElementById('cfg-segment_duration').value.trim(),
        image_duration: parseFloat(document.getElementById('cfg-image_duration').value),
        crf: parseInt(document.getElementById('cfg-crf').value, 10),
        max_resolution: document.getElementById('cfg-max_resolution').value.trim(),
        fps: parseInt(document.getElementById('cfg-fps').value, 10),
        canvas_mode: document.getElementById('cfg-canvas_mode').value,
        x264_preset: document.getElementById('cfg-x264_preset').value,
        ken_burns: document.getElementById('cfg-ken_burns').checked,
        keep_segments: parseInt(document.getElementById('cfg-keep_segments').value, 10),
        no_upload: document.getElementById('cfg-no_upload').checked,
        no_scan: document.getElementById('cfg-no_scan').checked,
        privacy: document.getElementById('cfg-privacy').value,
        playlist: document.getElementById('cfg-playlist').value.trim(),
        quiet_period: document.getElementById('cfg-quiet_period').value.trim(),
        poll_interval: document.getElementById('cfg-poll_interval').value.trim(),
        only_camera_photos: document.getElementById('cfg-only_camera_photos').checked,
        only_phone_videos: document.getElementById('cfg-only_phone_videos').checked,
      };

      try {
        const res = await fetch('/api/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const d = await res.json();
        if (res.ok && d.ok) {
          alert('✅ 配置已更新并成功保存至 config.json！');
          closeConfigModal();
          pollDashboard();
        } else {
          alert('❌ 保存配置失败: ' + (d.error || '未知错误'));
        }
      } catch (err) {
        alert('❌ 保存配置网络异常: ' + err.message);
      }
    }

    async function startScan() {
      const btn = document.getElementById('scan-btn');
      btn.disabled = true;
      btn.textContent = '🔄 正在请求扫描...';
      try {
        const res = await fetch('/api/scan/start', { method: 'POST' });
        const d = await res.json();
        if (res.ok && d.ok) {
          alert('✅ ' + (d.message || '全量扫描已启动'));
          pollDashboard();
        } else {
          alert('❌ 触发扫描失败: ' + (d.error || '未知错误'));
        }
      } catch (err) {
        alert('❌ 请求失败: ' + err.message);
      } finally {
        btn.disabled = false;
        btn.textContent = '🔄 开始全量扫描';
      }
    }

    async function rebuildSegment(id) {
      if (!confirm(`确定要重新构建分段 [${id.slice(0, 12)}] 吗？`)) return;
      try {
        const res = await fetch(`/api/segment/rebuild?id=${encodeURIComponent(id)}`, { method: 'POST' });
        const d = await res.json();
        if (res.ok && d.ok) {
          alert('✅ ' + (d.message || '分段重建已启动'));
          pollDashboard();
        } else {
          alert('❌ 重建失败: ' + (d.error || '未知错误'));
        }
      } catch (err) {
        alert('❌ 请求失败: ' + err.message);
      }
    }

    async function uploadSegment(id) {
      if (!confirm(`确定要上传本地视频文件到 YouTube 吗？`)) return;
      try {
        const res = await fetch(`/api/segment/upload?id=${encodeURIComponent(id)}`, { method: 'POST' });
        const d = await res.json();
        if (res.ok && d.ok) {
          alert('✅ ' + (d.message || '视频上传已启动'));
          pollDashboard();
        } else {
          alert('❌ 上传失败: ' + (d.error || '未知错误'));
        }
      } catch (err) {
        alert('❌ 请求失败: ' + err.message);
      }
    }

    async function deleteYoutubeVideo(id, videoId) {
      if (!confirm(`⚠️ 警告：确定要从 YouTube 删除视频 [${videoId}] 吗？此操作不可撤销！`)) return;
      try {
        const res = await fetch(`/api/segment/delete_youtube?id=${encodeURIComponent(id)}`, { method: 'POST' });
        const d = await res.json();
        if (res.ok && d.ok) {
          alert('✅ ' + (d.message || 'YouTube 视频已删除'));
          pollDashboard();
        } else {
          alert('❌ 删除失败: ' + (d.error || '未知错误'));
        }
      } catch (err) {
        alert('❌ 请求失败: ' + err.message);
      }
    }

    setInterval(pollDashboard, 2000);
    pollDashboard();
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
    <a href="/log">返回控制台与仪表盘</a>
    <div class="hint">3 秒后将自动跳转返回控制台...</div>
  </div>
  <script>
    setTimeout(function() { window.location.href = '/log'; }, 3000);
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
    <a href="/log">返回控制台重试</a>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------- Request Handler


class _RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress noisy HTTP access log lines to keep application log clean
        pass

    def _send_json(self, status: int, data: dict) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _send_html(self, status: int, html_str: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html_str.encode("utf-8"))

    def _serve_file(self, file_path: str, content_type: str) -> None:
        try:
            file_size = os.path.getsize(file_path)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            with open(file_path, "rb") as f:
                chunk_size = 64 * 1024
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except OSError:
            self.send_response(404)
            self.end_headers()

    def _serve_media_view(self, abs_path: str, file_id: str) -> None:
        ext = os.path.splitext(abs_path)[1].lower()

        # Check for HEIC/HEIF needing JPEG conversion
        if ext in (".heic", ".heif"):
            db_path = _server_state.get("db_path")
            cache_base = os.path.dirname(db_path) if db_path else "/tmp"
            cache_dir = os.path.join(cache_base, ".preview_cache")
            os.makedirs(cache_dir, exist_ok=True)
            cached_file = os.path.join(cache_dir, f"{file_id}.jpg")

            if not os.path.exists(cached_file):
                try:
                    from PIL import Image, ImageOps
                    import pillow_heif

                    pillow_heif.register_heif_opener()
                    with Image.open(abs_path) as im:
                        im = ImageOps.exif_transpose(im)
                        im.thumbnail((2560, 2560), Image.Resampling.LANCZOS)
                        if im.mode not in ("RGB", "L"):
                            im = im.convert("RGB")
                        tmp_cache = cached_file + f".{os.getpid()}.tmp"
                        im.save(tmp_cache, "JPEG", quality=85)
                        os.replace(tmp_cache, cached_file)
                except Exception as exc:
                    _logger.warning("failed converting HEIC %s: %s", abs_path, exc)
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"Failed converting image")
                    return

            self._serve_file(cached_file, "image/jpeg")
            return

        mime_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }
        content_type = mime_types.get(ext, "image/jpeg")
        self._serve_file(abs_path, content_type)

    def _serve_media_stream(self, abs_path: str) -> None:
        ext = os.path.splitext(abs_path)[1].lower()
        content_type = "video/quicktime" if ext in (".mov", ".qt") else "video/mp4"

        try:
            file_size = os.path.getsize(abs_path)
        except OSError:
            self.send_response(404)
            self.end_headers()
            return

        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if match:
                start = int(match.group(1))
                end_group = match.group(2)
                if end_group:
                    end = int(end_group)
                else:
                    # Serve up to 2MB per chunk for responsive buffering
                    end = min(start + 2 * 1024 * 1024 - 1, file_size - 1)
                end = min(end, file_size - 1)

                if start >= file_size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return

                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()

                with open(abs_path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    chunk_size = 64 * 1024
                    while remaining > 0:
                        read_len = min(remaining, chunk_size)
                        chunk = f.read(read_len)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
                return

        # No Range header
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(file_size))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "public, max-age=3600")
        self.end_headers()
        with open(abs_path, "rb") as f:
            chunk_size = 64 * 1024
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                self.wfile.write(chunk)

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

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            return {}
        try:
            raw = self.rfile.read(content_length).decode("utf-8")
            return json.loads(raw)
        except Exception:
            return {}

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/api/auth/submit":
            data = self._read_json_body()
            target = data.get("url") or data.get("code") or ""
            self._handle_auth_exchange(target)
            return

        if path == "/api/scan/start":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            ok, msg = coord.trigger_scan()
            self._send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if path == "/api/segment/rebuild":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            data = self._read_json_body()
            seg_id = query.get("id", [None])[0] or data.get("id") or data.get("segment_id")
            if not seg_id:
                self._send_json(400, {"ok": False, "error": "缺少参数: id"})
                return
            ok, msg = coord.rebuild_segment(seg_id)
            self._send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if path == "/api/segment/upload":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            data = self._read_json_body()
            seg_id = query.get("id", [None])[0] or data.get("id") or data.get("segment_id")
            if not seg_id:
                self._send_json(400, {"ok": False, "error": "缺少参数: id"})
                return
            ok, msg = coord.upload_segment(seg_id)
            self._send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if path == "/api/segment/delete_youtube":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            data = self._read_json_body()
            seg_id = query.get("id", [None])[0] or data.get("id") or data.get("segment_id")
            if not seg_id:
                self._send_json(400, {"ok": False, "error": "缺少参数: id"})
                return
            ok, msg = coord.delete_youtube_video(seg_id)
            self._send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if path == "/api/config":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            data = self._read_json_body()
            try:
                res = coord.update_config(data)
                self._send_json(200, {"ok": True, "config": res["config"]})
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            return

        self.send_response(404)
        self.end_headers()


    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Check for OAuth callback landing anywhere
        if "code" in query:
            code = query["code"][0]
            session = _server_state.get("oauth_session")
            if session:
                try:
                    session.exchange(code)
                    _server_state["auth_event"].set()
                    _server_state["auth_code_queue"].put(code)
                    _logger.info("web server intercepted and exchanged OAuth code")
                    self._send_html(200, _AUTH_SUCCESS_HTML)
                    return
                except Exception as exc:
                    _logger.error("OAuth exchange failed on callback: %s", exc)
                    _server_state["auth_error"] = str(exc)
                    self._send_html(400, _AUTH_ERROR_HTML.format(error=str(exc)))
                    return
            else:
                _server_state["auth_code_queue"].put(code)
                _logger.info("web server intercepted OAuth authorization code")
                self._send_html(200, _AUTH_SUCCESS_HTML)
                return

        # Route / -> Fullscreen Timeline Viewer
        if path == "/":
            self._send_html(200, _TIMELINE_VIEWER_HTML)
            return

        # Route /log and /logs -> Dashboard & Live Log Stream
        if path in ("/log", "/logs"):
            self._send_html(200, _LOG_DASHBOARD_HTML)
            return

        # Route /api/status
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

        # Route /api/dashboard
        if path == "/api/dashboard":
            self._api_dashboard()
            return

        # Route /api/media/summary
        if path == "/api/media/summary":
            self._api_media_summary()
            return

        # Route /api/media/items
        if path == "/api/media/items":
            self._api_media_items(query)
            return

        # Route /api/media/view?id=...
        if path == "/api/media/view":
            fid = query.get("id", [None])[0]
            if not fid:
                self.send_response(400)
                self.end_headers()
                return
            abs_path, _ = _resolve_media_path(fid)
            if not abs_path or not os.path.isfile(abs_path):
                self.send_response(404)
                self.end_headers()
                return
            self._serve_media_view(abs_path, fid)
            return

        # Route /api/media/stream?id=...
        if path == "/api/media/stream":
            fid = query.get("id", [None])[0]
            if not fid:
                self.send_response(400)
                self.end_headers()
                return
            abs_path, _ = _resolve_media_path(fid)
            if not abs_path or not os.path.isfile(abs_path):
                self.send_response(404)
                self.end_headers()
                return
            self._serve_media_stream(abs_path)
            return

        # Route /api/auth/submit (GET query parameter fallback)
        if path == "/api/auth/submit":
            target = query.get("url", [None])[0] or query.get("code", [None])[0] or ""
            self._handle_auth_exchange(target)
            return

        # Route /api/config
        if path == "/api/config":
            coord = get_app_coordinator()
            if coord:
                self._send_json(200, coord.get_config())
            else:
                from .coordinator import FINGERPRINT_PARAMS
                self._send_json(200, {
                    "config": {
                        "segment_duration": 3600.0,
                        "keep_segments": _server_state.get("keep_segments", 0),
                        "no_upload": False,
                        "no_scan": False,
                        "image_duration": 3.0,
                        "crf": 16,
                        "max_resolution": "7680x4320",
                        "canvas_mode": "max",
                        "fps": 60,
                        "x264_preset": "slow",
                        "ken_burns": False,
                        "privacy": "private",
                        "playlist": "",
                        "only_camera_photos": False,
                        "only_phone_videos": False,
                        "quiet_period": 600.0,
                        "poll_interval": 30.0,
                        "mtime_interval": 3600.0,
                        "quota_backoff": 3600.0,
                    },
                    "fingerprint_params": list(FINGERPRINT_PARAMS),
                })
            return

        # Route /api/logs
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
                    if is_initial and offset == 0 and file_size > 64 * 1024:
                        offset = max(0, file_size - 64 * 1024)

                    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(offset)
                        content = f.read()
                        new_offset = f.tell()
                except OSError as exc:
                    content = f"[无法读取日志文件: {exc}]\n"

            payload = {"offset": new_offset, "content": content}
            self._send_json(200, payload)
            return

        # Route /getbytime/{segment_id}/{minute}/{second}
        if path.startswith("/getbytime/"):
            parts = [p for p in path.strip("/").split("/") if p]
            if len(parts) >= 4 and parts[0] == "getbytime":
                self._handle_getbytime(parts[1], parts[2], parts[3], query)
                return

        # Route /api/segments
        if path == "/api/segments":
            self._api_segments()
            return

        # Route /api/segment/items
        if path == "/api/segment/items":
            self._api_segment_items(query)
            return

        self.send_response(404)
        self.end_headers()

    def _api_segments(self) -> None:
        db = get_db()
        if not db or not db.segments:
            self._send_json(200, {"segments": []})
            return

        segs = []
        for sid, srec in db.segments.items():
            segs.append({
                "id": sid,
                "title": srec.get("title") or sid,
                "file_count": len(srec.get("file_ids", [])),
                "duration_seconds": float(srec.get("duration_seconds") or 0.0),
                "status": srec.get("status") or "sealed",
                "range": srec.get("range") or [],
                "youtube_video_id": srec.get("youtube_video_id"),
            })

        def seg_sort_key(s):
            rng = s.get("range") or []
            return rng[0] if (rng and rng[0]) else s["title"]

        segs.sort(key=seg_sort_key)
        self._send_json(200, {"segments": segs})

    def _api_segment_items(self, query: dict) -> None:
        db = get_db()
        seg_id = query.get("id", [None])[0]
        if not db or not seg_id:
            self._send_json(400, {"ok": False, "error": "缺少分片 ID 或数据库未就绪"})
            return

        srec = db.segments.get(seg_id)
        if not srec:
            for s, r in db.segments.items():
                if s.startswith(seg_id) or (len(seg_id) >= 8 and s[:len(seg_id)].lower() == seg_id.lower()):
                    srec = r
                    seg_id = s
                    break

        if not srec:
            self._send_json(404, {"ok": False, "error": "未找到指定分片"})
            return

        file_ids = srec.get("file_ids", [])
        media_dir = _server_state.get("media_dir") or "."
        items = []
        for fid in file_ids:
            f = db.files.get(fid)
            if not f:
                continue
            rel_path = f.get("path") or ""
            abs_path = os.path.abspath(os.path.join(media_dir, rel_path))
            items.append({
                "id": fid,
                "name": f.get("name") or os.path.basename(rel_path),
                "path": rel_path,
                "abs_path": abs_path,
                "type": f.get("type", "image"),
                "captured_at": f.get("captured_at_utc") or "",
                "resolution": f.get("resolution") or "",
                "duration": float(f.get("duration_seconds") or 0.0),
                "size": int(f.get("size_bytes") or 0),
            })

        self._send_json(200, {
            "ok": True,
            "segment_id": seg_id,
            "title": srec.get("title") or seg_id,
            "duration_seconds": float(srec.get("duration_seconds") or 0.0),
            "status": srec.get("status") or "sealed",
            "youtube_video_id": srec.get("youtube_video_id"),
            "items": items,
        })

    def _handle_getbytime(self, seg_id: str, min_str: str, sec_str: str, query: dict) -> None:
        db = get_db()
        if not db or not db.segments:
            self._send_html(404, "<h2 style='color:#fff;background:#000;padding:40px;font-family:sans-serif;'>数据库未就绪或未找到任何分片</h2>")
            return

        try:
            minute = int(min_str)
            second = int(sec_str)
            target_sec = minute * 60 + second
        except (ValueError, TypeError):
            self._send_html(400, "<h2 style='color:#fff;background:#000;padding:40px;font-family:sans-serif;'>时间参数无效: 分钟与秒必须为整数</h2>")
            return

        target_sid = None
        seg_record = None
        if seg_id in db.segments:
            target_sid = seg_id
            seg_record = db.segments[seg_id]
        else:
            for s, rec in db.segments.items():
                if s.startswith(seg_id) or (len(seg_id) >= 8 and s[:len(seg_id)].lower() == seg_id.lower()):
                    target_sid = s
                    seg_record = rec
                    break

        if not seg_record or not target_sid:
            self._send_html(404, f"<h2 style='color:#fff;background:#000;padding:40px;font-family:sans-serif;'>未找到对应分片: {seg_id}</h2>")
            return

        file_ids = seg_record.get("file_ids", [])
        if not file_ids:
            self._send_html(404, f"<h2 style='color:#fff;background:#000;padding:40px;font-family:sans-serif;'>分片 {target_sid[:16]} 内无文件记录</h2>")
            return

        media_dir = _server_state.get("media_dir") or "."
        current_offset = 0.0
        hit_file_id = None
        hit_file_rec = None
        hit_item_start = 0.0
        hit_item_end = 0.0
        hit_item_index = 0

        for idx, fid in enumerate(file_ids):
            frec = db.files.get(fid, {})
            duration = float(frec.get("duration_seconds") or 3.0)
            item_start = current_offset
            item_end = current_offset + duration
            if item_start <= target_sec < item_end:
                hit_file_id = fid
                hit_file_rec = frec
                hit_item_start = item_start
                hit_item_end = item_end
                hit_item_index = idx + 1
                break
            current_offset += duration

        total_duration = current_offset
        if not hit_file_rec:
            total_m = int(total_duration // 60)
            total_s = int(total_duration % 60)
            self._send_html(
                404,
                f"<div style='background:#111;color:#fff;padding:40px;font-family:sans-serif;line-height:1.6;'>"
                f"<h2>⏱️ 超出分片总时长</h2>"
                f"<p>分片标题: <b>{seg_record.get('title', target_sid)}</b></p>"
                f"<p>查询时间: <b>{minute}分{second}秒</b> (第 {target_sec} 秒)</p>"
                f"<p>该分片总时长仅为: <b>{total_m}分{total_s:02d}秒</b> (共 {total_duration:.1f} 秒)</p>"
                f"<p><a href='/' style='color:#3b82f6;'>返回时间线画廊</a></p>"
                f"</div>"
            )
            return

        rel_path = hit_file_rec.get("path") or ""
        abs_path = os.path.abspath(os.path.join(media_dir, rel_path))
        ftype = hit_file_rec.get("type", "image")
        is_video = (ftype == "video")
        captured_at = hit_file_rec.get("captured_at_utc") or "未知"
        resolution = hit_file_rec.get("resolution") or "未知"
        item_dur = float(hit_file_rec.get("duration_seconds") or 3.0)

        # Raw redirect
        if query.get("raw", ["0"])[0] == "1":
            target_url = f"/api/media/stream?id={hit_file_id}" if is_video else f"/api/media/view?id={hit_file_id}"
            self.send_response(302)
            self.send_header("Location", target_url)
            self.end_headers()
            return

        # JSON response
        accept_header = self.headers.get("Accept", "")
        if query.get("format", [""])[0] == "json" or "application/json" in accept_header:
            self._send_json(200, {
                "ok": True,
                "segment_id": target_sid,
                "segment_title": seg_record.get("title", target_sid),
                "file_id": hit_file_id,
                "path": rel_path,
                "abs_path": abs_path,
                "type": ftype,
                "captured_at": captured_at,
                "resolution": resolution,
                "duration_seconds": item_dur,
                "time_offset_start": hit_item_start,
                "time_offset_end": hit_item_end,
                "item_index": hit_item_index,
                "total_items": len(file_ids),
                "view_url": f"/api/media/view?id={hit_file_id}",
                "stream_url": f"/api/media/stream?id={hit_file_id}",
            })
            return

        seg_title = seg_record.get("title", target_sid)
        media_html = ""
        if is_video:
            media_html = f"""
            <video controls autoplay playsinline style="max-width:100%;max-height:68vh;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,0.8);background:#000;">
                <source src="/api/media/stream?id={hit_file_id}" type="video/mp4">
                您的浏览器不支持视频播放
            </video>
            """
        else:
            media_html = f"""
            <img src="/api/media/view?id={hit_file_id}" alt="{rel_path}" style="max-width:100%;max-height:68vh;object-fit:contain;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,0.8);">
            """

        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{minute:02d}:{second:02d} - {os.path.basename(rel_path)} | TubeTape</title>
  <style>
    body {{
      margin: 0; padding: 0; background: #0c0d10; color: #f3f4f6;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    }}
    .container {{
      max-width: 1000px; margin: 0 auto; padding: 24px 16px;
    }}
    .header {{
      display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;
    }}
    .header h1 {{ margin: 0; font-size: 1.25rem; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 8px; }}
    .header a {{
      color: #93c5fd; text-decoration: none; font-size: 0.9rem; background: rgba(59,130,246,0.15);
      padding: 6px 14px; border-radius: 6px; border: 1px solid rgba(59,130,246,0.3); transition: all 0.2s;
    }}
    .header a:hover {{ background: rgba(59,130,246,0.3); }}
    .path-card {{
      background: #181920; border: 1px solid #2d303e; border-radius: 10px; padding: 18px 20px;
      margin-bottom: 22px; box-shadow: 0 4px 16px rgba(0,0,0,0.4);
    }}
    .path-title {{
      font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.05em; color: #9ca3af; margin-bottom: 8px;
      display: flex; align-items: center; justify-content: space-between;
    }}
    .path-value {{
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.95rem; color: #60a5fa; word-break: break-all; background: #0f1015;
      padding: 10px 14px; border-radius: 6px; border: 1px solid #232634; display: flex; align-items: center; justify-content: space-between; gap: 12px;
    }}
    .copy-btn {{
      background: #2563eb; color: #fff; border: none; padding: 5px 12px; border-radius: 4px;
      cursor: pointer; font-size: 0.8rem; font-weight: 600; white-space: nowrap; transition: background 0.2s;
    }}
    .copy-btn:hover {{ background: #1d4ed8; }}
    .meta-tags {{
      display: flex; flex-wrap: wrap; gap: 10px; margin-top: 14px; font-size: 0.82rem; color: #d1d5db;
    }}
    .tag {{
      background: rgba(255,255,255,0.06); padding: 4px 10px; border-radius: 4px; border: 1px solid rgba(255,255,255,0.1);
    }}
    .tag b {{ color: #fff; }}
    .media-container {{
      display: flex; justify-content: center; align-items: center; margin: 24px 0; min-height: 400px;
    }}
    .actions {{
      display: flex; justify-content: center; gap: 14px; margin-top: 20px;
    }}
    .action-btn {{
      background: #222530; color: #fff; border: 1px solid #374151; padding: 10px 20px;
      border-radius: 8px; text-decoration: none; font-size: 0.9rem; font-weight: 500;
      display: inline-flex; align-items: center; gap: 8px; transition: all 0.2s;
    }}
    .action-btn:hover {{ background: #374151; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <h1>🎬 TubeTape 素材定位 <span>{minute:02d}:{second:02d}</span></h1>
      <a href="/">← 返回时间线画廊</a>
    </div>

    <div class="path-card">
      <div class="path-title">
        <span>📁 真实磁盘物理路径 (Real File Path)</span>
        <span>分片: {seg_title}</span>
      </div>
      <div class="path-value">
        <span id="filePathText">{abs_path}</span>
        <button class="copy-btn" onclick="navigator.clipboard.writeText(document.getElementById('filePathText').innerText); this.innerText='已复制!'; setTimeout(()=>this.innerText='复制路径', 2000);">复制路径</button>
      </div>

      <div class="meta-tags">
        <span class="tag">类型: <b>{"视频 (Video)" if is_video else "照片 (Photo)"}</b></span>
        <span class="tag">拍摄时间: <b>{captured_at}</b></span>
        <span class="tag">分辨率: <b>{resolution}</b></span>
        <span class="tag">素材时长: <b>{item_dur:.1f} 秒</b></span>
        <span class="tag">在分片中的位置: <b>第 {hit_item_index} / {len(file_ids)} 个素材 ({int(hit_item_start//60)}:{int(hit_item_start%60):02d} ~ {int(hit_item_end//60)}:{int(hit_item_end%60):02d})</b></span>
      </div>
    </div>

    <div class="media-container">
      {media_html}
    </div>

    <div class="actions">
      <a href="/api/media/{'stream' if is_video else 'view'}?id={hit_file_id}" target="_blank" class="action-btn">
        🔍 在新标签页查看原始{"视频" if is_video else "原图"}
      </a>
      <a href="/" class="action-btn">
        📱 进入全屏抖音画廊
      </a>
    </div>
  </div>
</body>
</html>
"""
        self._send_html(200, html)

    def _api_dashboard(self) -> None:
        try:
            db = get_db()
            files_count = len(db.files) if db else _server_state["scanner"]["count"]

            # Count disk segments in uploaded_segments/
            disk_count = 0
            db_path = _server_state.get("db_path")
            if db_path:
                segments_dir = os.path.join(os.path.dirname(db_path), "uploaded_segments")
                if os.path.isdir(segments_dir):
                    for entry in os.scandir(segments_dir):
                        if entry.is_file() and entry.name.lower().endswith(".mp4") and not entry.name.startswith("."):
                            disk_count += 1

            # Combine db segments and planned segments
            segments_dict = {}
            if db:
                for sid, srec in list(db.segments.items()):
                    title = srec.get("title")
                    rng = srec.get("range", ["", ""])
                    if not title:
                        title = f"{rng[0]} - {rng[1]} [{sid[:16]}]" if rng[0] else sid[:16]
                    dur = float(srec.get("duration_seconds", 0.0))
                    segments_dict[sid] = {
                        "segment_id": sid,
                        "title": title,
                        "start_ts": rng[0],
                        "end_ts": rng[1],
                        "duration_seconds": dur,
                        "duration_text": _format_seconds(dur),
                        "file_count": len(srec.get("file_ids", [])),
                        "status": "uploaded" if srec.get("youtube_video_id") else srec.get("status", "sealed"),
                        "youtube_video_id": srec.get("youtube_video_id"),
                        "progress": None,
                    }

            # Planned segments
            for seg in _server_state.get("planned_segments", []):
                sid = getattr(seg, "segment_id", "")
                title = getattr(seg, "title", "")
                dur = float(getattr(seg, "duration_seconds", 0.0))
                fcount = len(getattr(seg, "file_ids", []))
                start_ts = getattr(seg, "start_ts", "")
                end_ts = getattr(seg, "end_ts", "")
                if sid not in segments_dict:
                    segments_dict[sid] = {
                        "segment_id": sid,
                        "title": title or f"{start_ts} - {end_ts}",
                        "start_ts": start_ts,
                        "end_ts": end_ts,
                        "duration_seconds": dur,
                        "duration_text": _format_seconds(dur),
                        "file_count": fcount,
                        "status": "pending",
                        "youtube_video_id": None,
                        "progress": None,
                    }

            # Current transcode
            cur_tc = _server_state.get("transcode", {})
            active_sid = cur_tc.get("segment_id")
            if active_sid and active_sid in segments_dict:
                segments_dict[active_sid]["status"] = "building"
                segments_dict[active_sid]["progress"] = {
                    "done": cur_tc.get("done", 0),
                    "total": cur_tc.get("total", 0),
                    "current_file": cur_tc.get("current_file", ""),
                }

            # Check has_local_file for each segment
            coord = get_app_coordinator()
            for sid, seg_info in segments_dict.items():
                has_local = False
                if coord:
                    has_local = coord.find_local_segment_file(sid, seg_info.get("title")) is not None
                elif db_path:
                    segments_dir = os.path.join(os.path.dirname(db_path), "uploaded_segments")
                    if os.path.isdir(segments_dir):
                        t = seg_info.get("title")
                        cands = [os.path.join(segments_dir, f"{sid}.mp4")]
                        if t:
                            cands.append(os.path.join(segments_dir, f"{t}.mp4"))
                        has_local = any(os.path.isfile(c) for c in cands)
                seg_info["has_local_file"] = has_local

            # Sort segments chronologically
            segments_list = list(segments_dict.values())
            segments_list.sort(key=lambda x: (x.get("start_ts") or "", x.get("title") or ""))

            uploaded_count = sum(1 for s in segments_list if s.get("youtube_video_id"))
            session = _server_state.get("oauth_session")
            auth_required = session is not None and not _server_state["auth_event"].is_set()

            payload = {
                "status": _server_state["status"],
                "task": _server_state["task"],
                "auth_required": auth_required,
                "auth_url": session.auth_url if auth_required else None,
                "scanner": _server_state["scanner"],
                "transcode": cur_tc,
                "stats": {
                    "total_files": files_count,
                    "total_segments": len(segments_list),
                    "uploaded_segments": uploaded_count,
                    "disk_segments_count": disk_count,
                    "keep_segments": _server_state.get("keep_segments", 0),
                    "no_upload": bool(getattr(coord.args, "no_upload", False)) if coord else False,
                },
                "segments": segments_list,
            }

            self._send_json(200, payload)
        except Exception as exc:
            _logger.exception("dashboard api error: %s", exc)
            self._send_json(500, {"error": str(exc)})

    def _api_media_summary(self) -> None:
        files = _get_sorted_media_files()
        total = len(files)

        # Build date groups
        groups_cache = _server_state.get("_date_groups_cache")
        if groups_cache is None:
            groups = []
            cur_month = None
            cur_group = None
            for idx, f in enumerate(files):
                cap = f["captured_at"]
                ym = cap[:7] if len(cap) >= 7 else "其他"
                if ym != cur_month:
                    cur_month = ym
                    cur_group = {
                        "year_month": ym,
                        "label": ym,
                        "start_index": idx,
                        "count": 1,
                    }
                    groups.append(cur_group)
                else:
                    cur_group["count"] += 1
            groups_cache = groups
            _server_state["_date_groups_cache"] = groups_cache

        payload = {
            "total": total,
            "date_groups": groups_cache,
        }
        self._send_json(200, payload)

    def _api_media_items(self, query: dict) -> None:
        files = _get_sorted_media_files()
        total = len(files)
        offset = 0
        limit = 50
        try:
            offset = max(0, int(query.get("offset", [0])[0]))
            limit = min(200, max(1, int(query.get("limit", [50])[0])))
        except (ValueError, TypeError):
            pass

        items_slice = files[offset : offset + limit]
        items_with_index = []
        for i, f in enumerate(items_slice):
            item = dict(f)
            item["index"] = offset + i
            items_with_index.append(item)

        payload = {
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": items_with_index,
        }
        self._send_json(200, payload)


# ---------------------------------------------------------------------- WebServer


class WebServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        log_file: str | None = None,
        media_dir: str | None = None,
        db_path: str | None = None,
        keep_segments: int = 0,
    ):
        self.host = host
        self.port = port
        _server_state["host"] = host
        _server_state["port"] = port
        if log_file:
            set_web_log_file(log_file)
        if media_dir:
            set_web_context(media_dir=media_dir, db_path=db_path, keep_segments=keep_segments)
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
