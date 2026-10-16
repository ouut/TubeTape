"""TubeTape Web Console and API Server.

Multi-page modular architecture:
- /: Dashboard (控制台)
- /view: View gallery (视界画廊 2D)
- /segments: Segments manager (分片管理与文件排除/纳入)
- /logs: Real-time logs (运行日志)
- /settings: Full settings editor (系统配置与重启应用)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .common import render_navbar
from .dashboard import render_dashboard_html
from .logs import render_logs_html
from .segments import render_segments_html
from .settings import render_settings_html
from .view import render_view_html

_logger = logging.getLogger("tubetape.web")

# Shared server state
_server_state = {
    "host": "0.0.0.0",
    "port": 8080,
    "is_running": False,
    "status": "idle",
    "task": "",
    "log_file": None,
    "media_dir": None,
    "segments_dir": None,
    "db_path": None,
    "db": None,
    "coordinator": None,
    "keep_segments": 0,
    "oauth_session": None,
    "auth_event": threading.Event(),
    "auth_error": None,
    "auth_code_queue": queue.Queue(),
    "scanner": {
        "is_scanning": False,
        "count": 0,
        "current": None,
        "mode": "full",
        "total": 0,
        "retained": 0,
    },
    "planned_segments": [],
    "transcode": {
        "segment_id": None,
        "title": None,
        "done": 0,
        "total": 0,
        "current_file": None,
        "resumed": False,
        "file_progress": None,
        "fps": None,
        "speed": None,
    },
    "upload": {
        "segment_id": None,
        "title": None,
        "percent": 0.0,
        "uploaded_bytes": 0,
        "total_bytes": 0,
        "speed": None,
    },
    "upload_queue": [],
    "_file_cache": None,
    "_file_cache_db_len": 0,
    "_date_groups_cache": None,
}


def set_app_coordinator(coordinator) -> None:
    _server_state["coordinator"] = coordinator


def get_app_coordinator():
    return _server_state.get("coordinator")


def is_running() -> bool:
    return _server_state["is_running"]


def get_web_port() -> int:
    return _server_state["port"]


def set_web_status(status: str, task: str = "") -> None:
    _server_state["status"] = status
    _server_state["task"] = task


def set_web_log_file(path: str) -> None:
    _server_state["log_file"] = os.path.abspath(path) if path else None


def set_web_context(
    media_dir: str | None = None,
    db=None,
    db_path: str | None = None,
    segments_dir: str | None = None,
    keep_segments: int = 0,
) -> None:
    if media_dir:
        _server_state["media_dir"] = os.path.abspath(media_dir)
    if db is not None:
        _server_state["db"] = db
    if db_path:
        _server_state["db_path"] = os.path.abspath(db_path)
    if segments_dir:
        _server_state["segments_dir"] = os.path.abspath(segments_dir)
    _server_state["keep_segments"] = keep_segments


def update_web_scanner(
    is_scanning: bool,
    count: int = 0,
    current: str | None = None,
    mode: str = "full",
    total: int = 0,
    retained: int = 0,
) -> None:
    _server_state["scanner"] = {
        "is_scanning": is_scanning,
        "count": count,
        "current": current,
        "mode": mode,
        "total": total,
        "retained": retained,
    }


def set_web_planned_segments(segments: list) -> None:
    _server_state["planned_segments"] = list(segments)


def update_web_transcode(
    segment_id: str,
    title: str | None = None,
    done: int = 0,
    total: int = 0,
    current_file: str | None = None,
    resumed: bool = False,
    file_progress: float | None = None,
    fps: float | None = None,
    speed: str | None = None,
) -> None:
    _server_state["transcode"] = {
        "segment_id": segment_id,
        "title": title or segment_id[:16],
        "done": done,
        "total": total,
        "current_file": current_file,
        "resumed": resumed,
        "file_progress": file_progress,
        "fps": fps,
        "speed": speed,
    }


def finish_web_segment(segment_id: str, youtube_video_id: str | None = None) -> None:
    if _server_state["transcode"].get("segment_id") == segment_id:
        _server_state["transcode"] = {
            "segment_id": None,
            "title": None,
            "done": 0,
            "total": 0,
            "current_file": None,
            "resumed": False,
            "file_progress": None,
            "fps": None,
            "speed": None,
        }
    if _server_state["upload"].get("segment_id") == segment_id:
        clear_web_upload()
    uq = _server_state.get("upload_queue", [])
    if segment_id in uq:
        _server_state["upload_queue"] = [sid for sid in uq if sid != segment_id]


def update_web_upload(
    segment_id: str,
    title: str | None = None,
    percent: float = 0.0,
    uploaded_bytes: int = 0,
    total_bytes: int = 0,
    speed: str | None = None,
) -> None:
    _server_state["upload"] = {
        "segment_id": segment_id,
        "title": title or segment_id[:16],
        "percent": max(0.0, min(100.0, float(percent))),
        "uploaded_bytes": int(uploaded_bytes),
        "total_bytes": int(total_bytes),
        "speed": speed,
    }


def clear_web_upload() -> None:
    _server_state["upload"] = {
        "segment_id": None,
        "title": None,
        "percent": 0.0,
        "uploaded_bytes": 0,
        "total_bytes": 0,
        "speed": None,
    }


def set_upload_queue(queue_sids: list[str]) -> None:
    _server_state["upload_queue"] = list(queue_sids)


set_web_upload_queue = set_upload_queue


def get_upload_queue() -> list[str]:
    return list(_server_state.get("upload_queue", []))


def set_web_oauth_session(session) -> None:
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


def set_web_auth_error(error_message: str) -> None:
    _server_state["auth_error"] = error_message
    _server_state["status"] = "error"
    _server_state["task"] = f"OAuth 授权失败: {error_message}"
    _server_state["auth_event"].set()


def get_auth_code(timeout: float = 300.0) -> str | None:
    try:
        return _server_state["auth_code_queue"].get(timeout=timeout)
    except queue.Empty:
        return None


def wait_for_auth(timeout: float = 3600.0):
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
    if session and getattr(session, "credentials", None):
        creds = session.credentials
        clear_web_oauth_session()
        return creds

    raise RuntimeError("OAuth 授权已完成但未找到有效凭据")


def get_db():
    coord = get_app_coordinator()
    if coord and getattr(coord, "db", None) is not None:
        return coord.db
    db = _server_state.get("db")
    if db is not None:
        return db
    db_path = _server_state.get("db_path")
    if db_path and os.path.exists(db_path):
        from ..db import Database
        try:
            db = Database.load(db_path)
            _server_state["db"] = db
            return db
        except Exception as exc:
            _logger.debug("could not load db from %s: %s", db_path, exc)
            return None
    return None


def _get_sorted_media_files() -> list[dict]:
    db = get_db()
    if not db or not getattr(db, "files", None):
        return []

    cache = _server_state.get("_file_cache")
    if cache is not None and _server_state.get("_file_cache_db_len") == len(db.files):
        return cache

    file_list = []
    for fid, f in db.files.items():
        file_list.append({
            "id": fid,
            "name": f.get("name") or os.path.basename(f.get("path", "")),
            "rel_path": f.get("path") or "",
            "type": f.get("type", "image"),
            "captured_at": f.get("captured_at_utc") or "",
            "resolution": f.get("resolution") or "",
            "duration": float(f.get("duration_seconds") or 0.0),
            "size": int(f.get("size_bytes") or 0),
        })

    def sort_key(item: dict):
        cap = item["captured_at"]
        return (0 if cap else 1, cap, item["rel_path"])

    file_list.sort(key=sort_key)
    _server_state["_file_cache"] = file_list
    _server_state["_file_cache_db_len"] = len(db.files)
    _server_state["_date_groups_cache"] = None
    return file_list


def _resolve_media_path(file_id: str) -> tuple[str | None, dict | None]:
    db = get_db()
    if not db or not getattr(db, "files", None) or file_id not in db.files:
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


def _format_segment_title(srec_or_seg, sid: str) -> str:
    title = getattr(srec_or_seg, "title", None) if not isinstance(srec_or_seg, dict) else srec_or_seg.get("title")
    if title:
        return title
    rng = getattr(srec_or_seg, "range", None) if isinstance(srec_or_seg, dict) else [
        getattr(srec_or_seg, "start_ts", ""),
        getattr(srec_or_seg, "end_ts", ""),
    ]
    if not rng:
        rng = [
            getattr(srec_or_seg, "start_ts", "") if hasattr(srec_or_seg, "start_ts") else srec_or_seg.get("start_ts", ""),
            getattr(srec_or_seg, "end_ts", "") if hasattr(srec_or_seg, "end_ts") else srec_or_seg.get("end_ts", ""),
        ]
    start = (rng[0] or "") if len(rng) > 0 else ""
    end = (rng[1] or "") if len(rng) > 1 else ""
    if start and end:
        return f"{start} - {end} [{sid[:16]}]"
    elif start:
        return f"{start} [{sid[:16]}]"
    return sid[:16]


def _collect_segments() -> dict[str, dict]:
    """Collect all segments from db.segments, planned_segments, and dynamic fallback."""
    db = get_db()
    segments_dict: dict[str, dict] = {}

    # 1. Sealed / committed segments from DB
    if db and getattr(db, "segments", None):
        for sid, srec in list(db.segments.items()):
            title = _format_segment_title(srec, sid)
            rng = srec.get("range", ["", ""])
            dur = float(srec.get("duration_seconds", 0.0))
            fids = list(srec.get("file_ids", []))
            segments_dict[sid] = {
                "segment_id": sid,
                "title": title,
                "start_ts": rng[0] if len(rng) > 0 else "",
                "end_ts": rng[1] if len(rng) > 1 else "",
                "range": rng,
                "duration_seconds": dur,
                "duration_text": _format_seconds(dur),
                "file_count": len(fids),
                "file_ids": fids,
                "status": "uploaded" if srec.get("youtube_video_id") else srec.get("status", "sealed"),
                "youtube_video_id": srec.get("youtube_video_id"),
                "progress": None,
            }

    # 2. In-memory planned segments
    planned = _server_state.get("planned_segments", [])
    for seg in planned:
        sid = getattr(seg, "segment_id", None) or (seg.get("segment_id") if isinstance(seg, dict) else "")
        if not sid:
            continue
        title = _format_segment_title(seg, sid)
        dur = float(getattr(seg, "duration_seconds", 0.0) if hasattr(seg, "duration_seconds") else (seg.get("duration_seconds", 0.0) if isinstance(seg, dict) else 0.0))
        fids = list(getattr(seg, "file_ids", []) if hasattr(seg, "file_ids") else (seg.get("file_ids", []) if isinstance(seg, dict) else []))
        start_ts = getattr(seg, "start_ts", "") if hasattr(seg, "start_ts") else (seg.get("start_ts", "") if isinstance(seg, dict) else "")
        end_ts = getattr(seg, "end_ts", "") if hasattr(seg, "end_ts") else (seg.get("end_ts", "") if isinstance(seg, dict) else "")

        if sid not in segments_dict:
            segments_dict[sid] = {
                "segment_id": sid,
                "title": title,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "range": [start_ts, end_ts],
                "duration_seconds": dur,
                "duration_text": _format_seconds(dur),
                "file_count": len(fids),
                "file_ids": fids,
                "status": "pending",
                "youtube_video_id": None,
                "progress": None,
            }

    # 3. Dynamic plan fallback: if no segments exist yet, but DB has files
    if not segments_dict and db and getattr(db, "files", None):
        try:
            from ..planner import plan
            from ..scanner import ScannedFile

            media_dir = _server_state.get("media_dir") or "."
            scanned_files = [ScannedFile.from_record(fid, rec, media_dir) for fid, rec in db.files.items()]
            coord = get_app_coordinator()
            seg_dur = None
            if coord and getattr(coord, "args", None):
                seg_dur = getattr(coord.args, "segment_duration", None)
            if seg_dur is None and db and getattr(db, "config", None):
                seg_dur = db.config.get("segment_duration")
            if seg_dur is None:
                seg_dur = 1200.0
            seg_dur = float(seg_dur)
            params = coord.segment_params() if coord else ()
            short_th = getattr(coord.args, "short_video_threshold", 300.0) if coord and getattr(coord, "args", None) else 300.0
            ign_lp = getattr(coord.args, "ignore_live_photos", True) if coord and getattr(coord, "args", None) else True
            min_vd = getattr(coord.args, "min_video_duration", 2.0) if coord and getattr(coord, "args", None) else 2.0
            plan_res = plan(
                scanned_files,
                getattr(db, "segments", {}),
                seg_dur,
                params=params,
                flush=True,
                short_video_threshold=short_th,
                ignore_live_photos=ign_lp,
                min_video_duration=min_vd,
            )
            for seg in plan_res.segments:
                sid = seg.segment_id
                if sid not in segments_dict:
                    segments_dict[sid] = {
                        "segment_id": sid,
                        "title": seg.title,
                        "start_ts": seg.start_ts,
                        "end_ts": seg.end_ts,
                        "range": [seg.start_ts, seg.end_ts],
                        "duration_seconds": seg.duration_seconds,
                        "duration_text": _format_seconds(seg.duration_seconds),
                        "file_count": len(seg.file_ids),
                        "file_ids": list(seg.file_ids),
                        "status": "pending",
                        "youtube_video_id": None,
                        "progress": None,
                    }
        except Exception as exc:
            _logger.debug("dynamic plan fallback failed: %s", exc)

    # 4. Active transcode progress overlay
    cur_tc = _server_state.get("transcode", {})
    active_sid = cur_tc.get("segment_id")
    if active_sid and active_sid in segments_dict:
        segments_dict[active_sid]["status"] = "building"
        segments_dict[active_sid]["progress"] = {
            "done": cur_tc.get("done", 0),
            "total": cur_tc.get("total", 0),
            "current_file": cur_tc.get("current_file", ""),
            "resumed": cur_tc.get("resumed", False),
            "file_progress": cur_tc.get("file_progress"),
            "fps": cur_tc.get("fps"),
            "speed": cur_tc.get("speed"),
        }

    # 5. Upload queue and active upload progress overlay
    cur_up = _server_state.get("upload", {})
    active_up_sid = cur_up.get("segment_id")
    upload_q = set(_server_state.get("upload_queue", []))

    for sid, seg in segments_dict.items():
        is_sealed_without_yt = (seg.get("status") in ("sealed", "uploaded")) and not seg.get("youtube_video_id")
        seg["is_pending_upload"] = is_sealed_without_yt
        seg["is_upload_queued"] = sid in upload_q
        if active_up_sid and sid == active_up_sid:
            seg["status"] = "uploading"
            seg["upload_progress"] = {
                "percent": cur_up.get("percent", 0.0),
                "uploaded_bytes": cur_up.get("uploaded_bytes", 0),
                "total_bytes": cur_up.get("total_bytes", 0),
                "speed": cur_up.get("speed"),
            }
        else:
            seg["upload_progress"] = None

    return segments_dict


class _RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        pass

    def _send_html(self, status: int, content: str) -> None:
        try:
            data = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except OSError as e:
            if getattr(e, "winerror", None) in (10053, 10054) or getattr(e, "errno", None) in (32, 104):
                pass
            else:
                raise

    def _send_json(self, status: int, data: dict | list) -> None:
        try:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except OSError as e:
            if getattr(e, "winerror", None) in (10053, 10054) or getattr(e, "errno", None) in (32, 104):
                pass
            else:
                raise

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            return {}
        try:
            raw = self.rfile.read(content_length).decode("utf-8")
            return json.loads(raw)
        except Exception:
            return {}

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        body_dict = self._read_json_body()

        if path == "/api/auth/submit":
            target = body_dict.get("url") or body_dict.get("code") or ""
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
                _server_state["status"] = "idle"
                _logger.info("OAuth authorization completed via manual submit")
                self._send_json(200, {"ok": True, "message": "授权成功，token.json 已保存"})
            except Exception as exc:
                _logger.warning("OAuth exchange failed: %s", exc)
                _server_state["auth_error"] = str(exc)
                self._send_json(400, {"ok": False, "error": str(exc)})
            return

        if path == "/api/scan/start":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            ok, msg = coord.trigger_scan()
            self._send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if path == "/api/sync_youtube":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            ok, msg, stats = coord.sync_youtube()
            self._send_json(200 if ok else 400, {"ok": ok, "message": msg, "stats": stats})
            return

        if path == "/api/segment/rebuild":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            seg_id = query.get("id", [None])[0] or query.get("segment_id", [None])[0] or body_dict.get("id") or body_dict.get("segment_id")
            if not seg_id:
                self._send_json(400, {"ok": False, "error": "缺少参数: segment_id"})
                return
            if hasattr(coord, "trigger_rebuild"):
                ok, msg = coord.trigger_rebuild(seg_id)
            else:
                ok, msg = coord.rebuild_segment(seg_id)
            self._send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if path == "/api/segment/upload":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            seg_id = query.get("id", [None])[0] or query.get("segment_id", [None])[0] or body_dict.get("id") or body_dict.get("segment_id")
            if not seg_id:
                self._send_json(400, {"ok": False, "error": "缺少参数: segment_id"})
                return
            ok, msg = coord.upload_segment(seg_id)
            self._send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if path == "/api/segment/toggle_file":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            seg_id = body_dict.get("segment_id")
            file_id = body_dict.get("file_id")
            is_excluded = bool(body_dict.get("excluded", False))
            if not seg_id or not file_id:
                self._send_json(400, {"ok": False, "error": "缺少 segment_id 或 file_id"})
                return
            ok, msg = coord.toggle_segment_file(seg_id, file_id, is_excluded)
            self._send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if path == "/api/segment/include_file":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            seg_id = body_dict.get("segment_id")
            file_id = body_dict.get("file_id")
            if not seg_id or not file_id:
                self._send_json(400, {"ok": False, "error": "缺少 segment_id 或 file_id"})
                return
            ok, msg = coord.include_candidate_file(seg_id, file_id)
            self._send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if path == "/api/segments/force_replan":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            ok, msg = coord.force_replan()
            self._send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if path in ("/api/config", "/api/config/apply"):
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            try:
                res = coord.update_config(body_dict)
                self._send_json(200, {"ok": True, "message": "配置已保存并平滑重新启动后台管道", "config": res.get("config", {})})
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            return

        if path == "/api/segment/delete_youtube":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            seg_id = query.get("id", [None])[0] or query.get("segment_id", [None])[0] or body_dict.get("id") or body_dict.get("segment_id")
            if not seg_id:
                self._send_json(400, {"ok": False, "error": "缺少参数: id"})
                return
            ok, msg = coord.delete_youtube_video(seg_id)
            self._send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if path == "/api/segment/delete_local":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            seg_id = query.get("id", [None])[0] or query.get("segment_id", [None])[0] or body_dict.get("id") or body_dict.get("segment_id")
            if not seg_id:
                self._send_json(400, {"ok": False, "error": "缺少参数: segment_id"})
                return
            ok, msg = coord.delete_local_segment_file(seg_id)
            self._send_json(200 if ok else 400, {"ok": ok, "message": msg})
            return

        if path == "/api/segment/split":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            seg_id = query.get("id", [None])[0] or query.get("segment_id", [None])[0] or body_dict.get("id") or body_dict.get("segment_id")
            if not seg_id:
                self._send_json(400, {"ok": False, "error": "缺少参数: segment_id"})
                return
            ok, msg, new_ids = coord.split_segment(seg_id)
            self._send_json(200 if ok else 400, {"ok": ok, "message": msg, "new_segment_ids": new_ids})
            return

        self._send_error_json(404, f"未找到 POST 接口: {path}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # 1. OAuth code redirect capture
        if "code" in query:
            code = query["code"][0]
            session = _server_state.get("oauth_session")
            if session:
                try:
                    session.exchange(code)
                    _server_state["auth_event"].set()
                    _server_state["auth_code_queue"].put(code)
                    _logger.info("web server intercepted and exchanged OAuth code")
                    html = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>TubeTape 授权成功</title>
                    <style>body { background: #0f172a; color: #fff; font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
                    .box { background: #1e293b; padding: 40px; border-radius: 12px; text-align: center; border: 1px solid rgba(255,255,255,0.1); }
                    a { color: #60a5fa; text-decoration: none; font-weight: bold; }
                    </style></head><body><div class="box"><h2>✅ Google 账号授权成功！</h2><p style="margin: 16px 0;">您可以关闭此页面，或者返回控制台。</p><p><a href="/">‹ 返回 TubeTape 控制台</a></p></div></body></html>"""
                    self._send_html(200, html)
                    return
                except Exception as exc:
                    _logger.error("OAuth exchange failed on callback: %s", exc)
                    _server_state["auth_error"] = str(exc)
                    self._send_html(400, f"<!DOCTYPE html><html><body><h2>OAuth 授权失败: {exc}</h2></body></html>")
                    return
            else:
                _server_state["auth_code_queue"].put(code)
                _logger.info("web server intercepted OAuth authorization code")
                html = """<!DOCTYPE html><html><body><h2>✅ 授权成功！</h2></body></html>"""
                self._send_html(200, html)
                return

        # 2. Main HTML Pages
        if path == "/":
            self._send_html(200, render_dashboard_html())
            return

        if path == "/view":
            self._send_html(200, render_view_html())
            return

        if path == "/segments":
            self._send_html(200, render_segments_html())
            return

        if path == "/log":
            self._send_html(200, _LOG_DASHBOARD_HTML)
            return

        if path == "/logs":
            self._send_html(200, render_logs_html())
            return

        if path == "/settings":
            self._send_html(200, render_settings_html())
            return

        # 3. Status & Dashboard APIs
        if path == "/api/sync_youtube":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            ok, msg, stats = coord.sync_youtube()
            self._send_json(200 if ok else 400, {"ok": ok, "message": msg, "stats": stats})
            return

        if path == "/api/status":
            session = _server_state.get("oauth_session")
            auth_req = session is not None and not _server_state["auth_event"].is_set()
            resp = {
                "status": "waiting_auth" if auth_req else _server_state["status"],
                "task": _server_state["task"],
                "auth_required": auth_req,
                "auth_url": getattr(session, "auth_url", None) if auth_req else None,
                "auth_error": _server_state.get("auth_error"),
            }
            self._send_json(200, resp)
            return

        if path == "/api/dashboard":
            session = _server_state.get("oauth_session")
            auth_req = session is not None and not _server_state["auth_event"].is_set()
            auth_url = getattr(session, "auth_url", None) if auth_req else None
            coord = get_app_coordinator()
            db = get_db()

            scanner_info = _server_state.get("scanner", {})
            if scanner_info.get("is_scanning") and scanner_info.get("mode") == "incremental":
                files_count = scanner_info.get("retained", 0) + scanner_info.get("total", 0)
            else:
                files_count = len(db.files) if db and getattr(db, "files", None) else scanner_info.get("count", 0)

            disk_count = 0
            db_path = _server_state.get("db_path")
            if db_path:
                segments_dir = os.path.join(os.path.dirname(db_path), "uploaded_segments")
                if os.path.isdir(segments_dir):
                    for entry in os.scandir(segments_dir):
                        if entry.is_file() and entry.name.lower().endswith(".mp4") and not entry.name.startswith("."):
                            disk_count += 1

            segments_dict = _collect_segments()

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

            segments_list = list(segments_dict.values())
            segments_list.sort(key=lambda x: (x.get("start_ts") or "", x.get("title") or ""))

            uploaded_count = sum(1 for s in segments_list if s.get("youtube_video_id"))
            uploaded_files_count = sum(s.get("file_count", 0) for s in segments_list if s.get("youtube_video_id"))
            pending_segments_count = len(segments_list) - uploaded_count
            pending_files_count = sum(s.get("file_count", 0) for s in segments_list if not s.get("youtube_video_id"))
            total_segment_files = sum(s.get("file_count", 0) for s in segments_list)
            overlap_files = max(0, total_segment_files - files_count)

            resp = {
                "status": "waiting_auth" if auth_req else _server_state["status"],
                "task": _server_state["task"],
                "auth_required": auth_req,
                "auth_url": auth_url,
                "auth": {
                    "required": auth_req,
                    "auth_url": auth_url,
                    "error": _server_state.get("auth_error"),
                },
                "scanner": _server_state["scanner"],
                "transcode": _server_state["transcode"],
                "upload": _server_state["upload"],
                "upload_queue": _server_state["upload_queue"],
                "media_dir": _server_state["media_dir"] or (coord.args.input if coord else None),
                "segments_dir": _server_state.get("segments_dir") or (coord.segments_dir if coord else None),
                "db_path": _server_state["db_path"] or (coord.db.path if coord and coord.db else None),
                "stats": {
                    "total_files": files_count,
                    "total_segments": len(segments_list),
                    "uploaded_segments": uploaded_count,
                    "uploaded_files_count": uploaded_files_count,
                    "pending_segments": pending_segments_count,
                    "pending_files_count": pending_files_count,
                    "total_segment_files": total_segment_files,
                    "overlap_files": overlap_files,
                    "disk_segments_count": disk_count,
                    "keep_segments": _server_state.get("keep_segments", 0),
                    "no_upload": bool(getattr(coord.args, "no_upload", False)) if coord else False,
                },
                "segments": segments_list,
            }
            self._send_json(200, resp)
            return

        # 4. Media & Segments Data APIs
        if path == "/api/media/summary":
            files = _get_sorted_media_files()
            total = len(files)

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

            coord = get_app_coordinator()
            if coord:
                stats = coord.get_media_stats()
            else:
                db = get_db()
                stats = {
                    "total_files": total,
                    "total_size_bytes": sum(f.get("size", 0) for f in files),
                    "total_size_formatted": "0 B",
                    "photos_count": sum(1 for f in files if f.get("type") == "image"),
                    "videos_count": sum(1 for f in files if f.get("type") == "video"),
                    "skipped_count": 0,
                    "date_span": f"{files[0]['captured_at'][:10]} ~ {files[-1]['captured_at'][:10]}" if files else "暂无数据",
                    "total_segments": len(db.segments) if db and getattr(db, "segments", None) else 0,
                    "uploaded_segments": sum(1 for s in (db.segments.values() if db and getattr(db, "segments", None) else []) if s.get("youtube_video_id")),
                }

            payload = {
                "total": total,
                "date_groups": groups_cache,
                "stats": stats,
            }
            self._send_json(200, payload)
            return

        if path == "/api/segments":
            coord = get_app_coordinator()
            if coord:
                seg_view_data = coord.get_segments_view_data()
                for s in seg_view_data:
                    s["id"] = s["segment_id"]
                    if "file_count" not in s:
                        s["file_count"] = len(s.get("files", []))
                self._send_json(200, {"segments": seg_view_data})
                return

            segments_dict = _collect_segments()
            db = get_db()
            segs = []
            for sid, srec in segments_dict.items():
                fids = srec.get("file_ids", [])
                file_items = []
                if db and getattr(db, "files", None):
                    for fid in fids:
                        f = db.files.get(fid)
                        if f:
                            file_items.append({
                                "file_id": fid,
                                "id": fid,
                                "name": f.get("name") or os.path.basename(f.get("path", "")),
                                "path": f.get("path", ""),
                                "type": f.get("type", "image"),
                                "captured_at_utc": f.get("captured_at_utc", ""),
                                "duration_seconds": f.get("duration_seconds", 3.0),
                                "resolution": f.get("resolution", ""),
                                "size_bytes": f.get("size_bytes", 0),
                                "is_excluded": False,
                            })
                segs.append({
                    "id": sid,
                    "segment_id": sid,
                    "title": srec.get("title") or sid[:16],
                    "file_count": srec.get("file_count", len(fids)),
                    "files": file_items,
                    "candidate_files": [],
                    "active_files_count": len(file_items),
                    "duration_seconds": float(srec.get("duration_seconds") or 0.0),
                    "status": srec.get("status") or "pending",
                    "range": srec.get("range") or [srec.get("start_ts", ""), srec.get("end_ts", "")],
                    "start_ts": srec.get("start_ts", ""),
                    "end_ts": srec.get("end_ts", ""),
                    "has_local_file": srec.get("has_local_file", False),
                    "youtube_video_id": srec.get("youtube_video_id"),
                })
            def seg_sort_key(s):
                rng = s.get("range") or []
                return rng[0] if (rng and rng[0]) else s["title"]
            segs.sort(key=seg_sort_key)
            self._send_json(200, {"segments": segs})
            return

        if path == "/api/segment/items":
            seg_id = query.get("id", [None])[0] or query.get("segment_id", [None])[0]
            if not seg_id:
                self._send_json(400, {"ok": False, "error": "缺少分片 ID"})
                return

            segments_dict = _collect_segments()
            srec = segments_dict.get(seg_id)
            if not srec:
                for s, r in segments_dict.items():
                    if s.startswith(seg_id) or (len(seg_id) >= 8 and s[:len(seg_id)].lower() == seg_id.lower()):
                        srec = r
                        seg_id = s
                        break

            if not srec:
                self._send_json(404, {"ok": False, "error": "未找到指定分片"})
                return

            db = get_db()
            file_ids = srec.get("file_ids", [])
            media_dir = _server_state.get("media_dir") or "."
            items = []
            if db and getattr(db, "files", None):
                for fid in file_ids:
                    f = db.files.get(fid)
                    if not f:
                        continue
                    rel_path = f.get("path") or ""
                    abs_path = os.path.abspath(os.path.join(media_dir, rel_path))
                    items.append({
                        "id": fid,
                        "file_id": fid,
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
                "title": srec.get("title") or seg_id[:16],
                "duration_seconds": float(srec.get("duration_seconds") or 0.0),
                "status": srec.get("status") or "sealed",
                "youtube_video_id": srec.get("youtube_video_id"),
                "items": items,
            })
            return

        if path == "/api/media/items":
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
            return

        # 5. File Streaming and Viewing API
        if path in ("/api/media/stream", "/api/media/view"):
            fid = query.get("id", [None])[0]
            if not fid:
                self.send_response(400)
                self.end_headers()
                return
            full_p, frec = _resolve_media_path(fid)
            if not full_p or not os.path.isfile(full_p):
                self.send_response(404)
                self.end_headers()
                return

            ext = os.path.splitext(full_p)[1].lower()
            mime = "application/octet-stream"
            if ext in (".jpg", ".jpeg"): mime = "image/jpeg"
            elif ext == ".png": mime = "image/png"
            elif ext == ".webp": mime = "image/webp"
            elif ext in (".mp4", ".m4v"): mime = "video/mp4"
            elif ext in (".mov", ".qt"): mime = "video/quicktime"
            elif ext == ".3gp": mime = "video/3gpp"
            elif ext == ".webm": mime = "video/webm"
            elif ext == ".mkv": mime = "video/x-matroska"
            elif ext == ".avi": mime = "video/x-msvideo"
            elif ext in (".mts", ".m2ts"): mime = "video/mp2t"

            video_exts = (".mp4", ".m4v", ".mov", ".qt", ".3gp", ".webm", ".mkv", ".avi", ".mts", ".m2ts")
            range_header = self.headers.get("Range")
            if range_header and ext in video_exts:
                try:
                    file_size = os.path.getsize(full_p)
                    match = re.match(r"bytes=(\d+)-(\d*)", range_header)
                    if match:
                        start = int(match.group(1))
                        end_group = match.group(2)
                        end = int(end_group) if end_group else min(start + 2 * 1024 * 1024 - 1, file_size - 1)
                        end = min(end, file_size - 1)

                        if start >= file_size:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{file_size}")
                            self.end_headers()
                            return

                        length = end - start + 1
                        self.send_response(206)
                        self.send_header("Content-Type", mime)
                        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                        self.send_header("Content-Length", str(length))
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Cache-Control", "public, max-age=3600")
                        self.end_headers()

                        with open(full_p, "rb") as f:
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
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                    return
                except OSError as e:
                    if getattr(e, "winerror", None) in (10053, 10054) or getattr(e, "errno", None) in (32, 104):
                        return
                    _logger.debug("stream range error: %s", e)
                    return
                except Exception as e:
                    _logger.debug("stream range error: %s", e)
                    return

            try:
                size = os.path.getsize(full_p)
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                with open(full_p, "rb") as f:
                    while chunk := f.read(65536):
                        self.wfile.write(chunk)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                return
            except OSError as e:
                if getattr(e, "winerror", None) in (10053, 10054) or getattr(e, "errno", None) in (32, 104):
                    return
                _logger.debug("stream file %s error: %s", full_p, e)
            except Exception as e:
                _logger.debug("stream file %s error: %s", full_p, e)
            return

        # 6. Legacy getbytime API (preserved for backward compatibility)
        if path.startswith("/getbytime/"):
            parts = [p for p in path.strip("/").split("/") if p]
            if len(parts) >= 4 and parts[0] == "getbytime":
                self._handle_getbytime(parts[1], parts[2], parts[3], query)
                return

        # 7. Config API
        if path == "/api/config":
            coord = get_app_coordinator()
            if not coord:
                self._send_json(400, {"ok": False, "error": "后台协调器未初始化"})
                return
            self._send_json(200, coord.get_config())
            return

        # 8. Logs API
        if path == "/api/logs":
            log_file = _server_state.get("log_file")
            if not log_file or not os.path.isfile(log_file):
                self._send_json(200, {"content": "", "offset": 0})
                return

            if "download" in query:
                try:
                    with open(log_file, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="tubetape.log"')
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as e:
                    self._send_error_json(500, f"下载日志出错: {e}")
                return

            try:
                offset = int(query.get("offset", [0])[0])
            except ValueError:
                offset = 0

            try:
                with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(offset)
                    content = f.read()
                    new_offset = f.tell()
                self._send_json(200, {"content": content, "offset": new_offset})
            except Exception as e:
                self._send_json(200, {"content": f"读取日志出错: {e}", "offset": offset})
            return

        self._send_error_json(404, f"未找到页面或接口: {path}")

    def _handle_getbytime(self, seg_id: str, min_str: str, sec_str: str, query: dict) -> None:
        segments_dict = _collect_segments()
        if not segments_dict:
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
        if seg_id in segments_dict:
            target_sid = seg_id
            seg_record = segments_dict[seg_id]
        else:
            for s, rec in segments_dict.items():
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

        db = get_db()
        media_dir = _server_state.get("media_dir") or "."
        current_offset = 0.0
        hit_file_id = None
        hit_file_rec = None
        hit_item_start = 0.0
        hit_item_end = 0.0
        hit_item_index = 0

        for idx, fid in enumerate(file_ids):
            frec = db.files.get(fid, {}) if (db and getattr(db, "files", None)) else {}
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
            media_html = f'''
            <video controls autoplay playsinline style="max-width:100%;max-height:68vh;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,0.8);background:#000;">
                <source src="/api/media/stream?id={hit_file_id}" type="video/mp4">
                您的浏览器不支持视频播放
            </video>
            '''
        else:
            media_html = f'''
            <img src="/api/media/view?id={hit_file_id}" alt="{rel_path}" style="max-width:100%;max-height:68vh;object-fit:contain;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,0.8);">
            '''

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
      display: flex; justify-content: space-between;
    }}
    .path-value {{
      font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
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
        <span class="tag">类型: <b>{'视频 (Video)' if is_video else '照片 (Photo)'}</b></span>
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
        🔍 在新标签页查看原始{'视频' if is_video else '原图'}
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


class _TubeTapeHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        exc_type, exc_val, _ = sys.exc_info()
        if exc_type and (
            issubclass(exc_type, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError))
            or (
                issubclass(exc_type, OSError)
                and (
                    getattr(exc_val, "winerror", None) in (10053, 10054)
                    or getattr(exc_val, "errno", None) in (32, 104)
                )
            )
        ):
            _logger.debug("client disconnected: %s (%s)", client_address, exc_val)
            return
        super().handle_error(request, client_address)


class WebServer:
    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        log_file: str | None = None,
        media_dir: str | None = None,
        db_path: str | None = None,
        keep_segments: int = 0,
    ) -> None:
        self.host = host
        self.port = port
        self.requested_port = port
        self.log_file = log_file
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

        if log_file:
            set_web_log_file(log_file)
        set_web_context(media_dir=media_dir, db_path=db_path, keep_segments=keep_segments)

    def start(self) -> None:
        if self._server is not None:
            return

        try:
            server = _TubeTapeHTTPServer((self.host, self.requested_port), _RequestHandler)
            actual_port = server.server_port
            self.port = actual_port
            _server_state["port"] = actual_port
            _server_state["host"] = self.host
            _server_state["is_running"] = True
            self._server = server

            def _serve() -> None:
                _logger.info("web console listening on http://%s:%d", self.host, actual_port)
                try:
                    server.serve_forever()
                except Exception:
                    pass
                finally:
                    _server_state["is_running"] = False

            self._thread = threading.Thread(target=_serve, daemon=True, name="WebServer")
            self._thread.start()
        except OSError as exc:
            _logger.warning("could not start web console on port %d: %s", self.requested_port, exc)
            self._server = None
            _server_state["is_running"] = False

    def stop(self) -> None:
        _server_state["is_running"] = False
        _server_state["planned_segments"] = []
        _server_state["coordinator"] = None
        _server_state["db"] = None
        _server_state["_file_cache"] = None
        _server_state["_date_groups_cache"] = None
        _server_state["transcode"] = {
            "segment_id": None,
            "title": None,
            "done": 0,
            "total": 0,
            "current_file": None,
            "resumed": False,
            "file_progress": None,
            "fps": None,
            "speed": None,
        }
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
            _logger.info("web console stopped")
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def start_server(
    host: str = "0.0.0.0",
    port: int = 8080,
    log_file: str | None = None,
    media_dir: str | None = None,
    db_path: str | None = None,
    keep_segments: int = 0,
) -> WebServer:
    server = WebServer(
        host=host,
        port=port,
        log_file=log_file,
        media_dir=media_dir,
        db_path=db_path,
        keep_segments=keep_segments,
    )
    server.start()
    return server


def stop_server(server: WebServer | None = None) -> None:
    if server is not None:
        server.stop()


# Backward-compatibility variable exports
_LOG_DASHBOARD_HTML = """<!-- TubeTape 运行控制台与仪表盘 时间线分段列表 实时运行日志 -->
""" + render_dashboard_html() + "\n" + render_logs_html()

_GALLERY_HTML = render_view_html()
