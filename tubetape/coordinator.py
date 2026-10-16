"""Central coordinator for TubeTape runtime operations.

Manages pipeline runs, background tasks (scan, rebuild, upload, delete),
and dynamic runtime configuration with persistence to tubetape.json.
"""

from __future__ import annotations

import bisect
import json
import logging
import os
import threading
import time
from typing import Any

from . import durations
from .chapters import build_segment_description, chapters_text
from .db import SEGMENT_STATUS_FAILED, SEGMENT_STATUS_SEALED, Database
from .planner import ScannedFile, _make_segment, greedy_pack, plan
from .rebuild import Rebuilder
from .reconcile import fetch_remote_catalog, fetch_remote_index, reconcile_database
from .scanner import FILE_TYPE_VIDEO, VIDEO_EXTENSIONS, scan
from .transcoder import TranscodeConfig, transcode_segment

_logger = logging.getLogger("tubetape.coordinator")

# Parameters that directly determine the segment_id hash fingerprint
FINGERPRINT_PARAMS = frozenset([
    "segment_duration",
    "image_duration",
    "crf",
    "max_resolution",
    "canvas_mode",
    "fps",
    "x264_preset",
    "ken_burns",
    "short_video_threshold",
    "ignore_live_photos",
    "min_video_duration",
])


def rotate_uploaded_segments(directory: str, keep_count: int) -> list[str]:
    """Maintain at most `keep_count` newest video files in `directory`.

    If keep_count <= 0, deletes all non-hidden video files.
    Returns list of deleted file paths.
    """
    if not os.path.isdir(directory):
        return []

    valid_exts = VIDEO_EXTENSIONS | {".mp4"}
    video_files = []
    for entry in os.scandir(directory):
        if (
            entry.is_file()
            and os.path.splitext(entry.name)[1].lower() in valid_exts
            and not entry.name.startswith(".")
        ):
            try:
                video_files.append((entry.path, entry.stat().st_mtime))
            except OSError:
                continue

    # Sort descending by mtime (newest first)
    video_files.sort(key=lambda x: x[1], reverse=True)

    deleted = []
    to_delete = video_files[max(0, keep_count):]
    for path, _ in to_delete:
        try:
            os.remove(path)
            deleted.append(path)
            _logger.info("removed old segment video: %s (keeping latest %d)", os.path.basename(path), keep_count)
        except OSError as exc:
            _logger.warning("could not delete old segment video %s: %s", path, exc)

    return deleted


class AppCoordinator:
    """Coordinates pipeline execution, background tasks, and dynamic config."""

    def __init__(self, args, db: Database | None = None, uploader=None):
        self.args = args
        self.db = db
        self.uploader = uploader
        self.task_lock = threading.Lock()
        self.current_task: str | None = None
        self._db_path = getattr(args, "db", None)
        self.segments_dir = (
            getattr(args, "segments_dir", None)
            or (os.path.join(os.path.dirname(args.db), "uploaded_segments") if getattr(args, "db", None) else "uploaded_segments")
        )
        os.makedirs(self.segments_dir, exist_ok=True)

    def load_saved_config(self) -> dict:
        """Load configuration overrides from tubetape.json database."""
        if self.db is None and self._db_path:
            try:
                self.db = Database.load(self._db_path)
            except Exception as exc:
                _logger.warning("could not load db from %s: %s", self._db_path, exc)
                return {}

        if not self.db:
            return {}

        saved = self.db.config
        explicit = getattr(self.args, "_explicit_cli_args", set())
        if saved:
            filtered_saved = {k: v for k, v in saved.items() if k not in explicit}
            self._apply_dict_to_args(filtered_saved)
            _logger.info("loaded dynamic config overrides from database %s", self.db.path)
        # Always persist current effective configuration to tubetape.json (except in dry-run mode)
        if not getattr(self.args, "dry_run", False):
            self.save_config()
        return self.db.config

    def save_config(self) -> None:
        """Persist current configuration to tubetape.json database."""
        if getattr(self.args, "dry_run", False):
            return

        if self.db is None and self._db_path:
            try:
                self.db = Database.load(self._db_path)
            except Exception as exc:
                _logger.warning("could not load db from %s: %s", self._db_path, exc)
                return

        if not self.db:
            return

        try:
            cfg = self.get_config()["config"]
            cfg["force_scan"] = False  # never persist force_scan across restarts
            cfg["force_plan"] = False  # never persist force_plan across restarts
            # Do not persist CLI one-time --no-upload flag to database
            if "no_upload" in getattr(self.args, "_explicit_cli_args", set()):
                cfg["no_upload"] = False
            self.db.config = cfg
            self.db.save()
            # Clean up legacy config.json if present
            if self._db_path:
                legacy = os.path.join(os.path.dirname(os.path.abspath(self._db_path)), "config.json")
                if os.path.isfile(legacy):
                    try:
                        os.remove(legacy)
                        _logger.info("cleaned up legacy %s after merging into database", legacy)
                    except OSError:
                        pass
            _logger.info("saved configuration to database %s", self.db.path)
        except Exception as exc:
            _logger.warning("could not save configuration to %s: %s", getattr(self.db, "path", self._db_path), exc)

    def get_config(self) -> dict[str, Any]:
        """Return full configuration dict and fingerprint sensitive fields."""
        a = self.args
        max_res_str = (
            f"{a.max_resolution[0]}x{a.max_resolution[1]}"
            if isinstance(a.max_resolution, (tuple, list))
            else str(a.max_resolution)
        )
        return {
            "config": {
                "input": str(getattr(a, "input", ".")),
                "segments_dir": str(getattr(a, "segments_dir", None) or self.segments_dir),
                "db": str(getattr(a, "db", "tubetape.json")),
                "log_file": str(getattr(a, "log_file", None) or (getattr(a, "db", "tubetape.json") + ".log")),
                "web_port": int(getattr(a, "web_port", 8080)),
                "keep_segments": int(getattr(a, "keep_segments", 0)),
                "no_upload": bool(getattr(a, "no_upload", False)),
                "force_scan": bool(getattr(a, "force_scan", False)),
                "force_plan": bool(getattr(a, "force_plan", False)),
                "segment_duration": float(getattr(a, "segment_duration", 1200.0)),
                "encoder": str(getattr(a, "encoder", "auto")),
                "crf": int(getattr(a, "crf", 18)),
                "max_resolution": max_res_str,
                "canvas_mode": str(getattr(a, "canvas_mode", "max")),
                "fps": int(getattr(a, "fps", 30)),
                "x264_preset": str(getattr(a, "x264_preset", "fast")),
                "image_duration": float(getattr(a, "image_duration", 3.0)),
                "ken_burns": bool(getattr(a, "ken_burns", False)),
                "short_video_threshold": float(getattr(a, "short_video_threshold", 300.0)),
                "min_video_duration": float(getattr(a, "min_video_duration", 2.0)),
                "ignore_live_photos": bool(getattr(a, "ignore_live_photos", True)),
                "only_camera_photos": bool(getattr(a, "only_camera_photos", False)),
                "only_phone_videos": bool(getattr(a, "only_phone_videos", False)),
                "timezone": str(getattr(a, "timezone", "Asia/Shanghai")),
                "privacy": str(getattr(a, "privacy", "private")),
                "playlist": str(getattr(a, "playlist", "") or ""),
                "watch": bool(getattr(a, "watch", True)),
                "quiet_period": float(getattr(a, "quiet_period", 600.0)),
                "poll_interval": float(getattr(a, "poll_interval", 30.0)),
                "mtime_interval": float(getattr(a, "mtime_interval", 600.0)),
                "quota_backoff": float(getattr(a, "quota_backoff", 3600.0)),
            },
            "fingerprint_params": list(FINGERPRINT_PARAMS),
        }

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Validate, apply, and persist configuration updates, then restart pipeline."""
        if isinstance(updates, dict) and "updates" in updates and isinstance(updates["updates"], dict):
            updates = updates["updates"]
        self._apply_dict_to_args(updates)
        self.save_config()

        # Update web server state
        from . import web

        if "keep_segments" in updates:
            web._server_state["keep_segments"] = self.args.keep_segments
            rotate_uploaded_segments(self.segments_dir, self.args.keep_segments)

        self.restart_pipeline()
        return self.get_config()

    def restart_pipeline(self) -> tuple[bool, str]:
        """Re-run pipeline in background with updated configuration."""
        if not self.task_lock.acquire(blocking=False):
            return False, f"已有任务正在运行中 ({self.current_task})，新配置已落盘，待当前任务结束后生效"

        self.current_task = "应用新配置并重新构建"

        def _worker():
            from . import cli, web

            try:
                _logger.info("re-running pipeline with updated configuration")
                web.set_web_status("scanning", "配置已更新，正在按新配置重新核对并运行...")
                cli.run_pipeline(self.args, force_scan=False)
            except Exception as exc:
                _logger.error("pipeline re-run failed: %s", exc, exc_info=True)
                web.set_web_status("error", f"重新运行出错: {exc}")
            finally:
                self.current_task = None
                self.task_lock.release()

        threading.Thread(target=_worker, daemon=True).start()
        return True, "已按照新配置重新启动流水线"

    def _apply_dict_to_args(self, updates: dict[str, Any]) -> None:
        a = self.args
        for k, v in updates.items():
            if v is None:
                continue
            if k == "segment_duration":
                val = durations.parse_duration(v) if isinstance(v, str) else float(v)
                if val <= 0:
                    raise ValueError("segment_duration 必须大于 0")
                a.segment_duration = val
            elif k in ("web_port", "port"):
                a.web_port = int(v)
            elif k == "segments_dir":
                if v and str(v).strip():
                    a.segments_dir = os.path.abspath(str(v).strip())
                    self.segments_dir = a.segments_dir
                    os.makedirs(self.segments_dir, exist_ok=True)
            elif k == "image_duration":
                val = float(v)
                if val <= 0:
                    raise ValueError("image_duration 必须大于 0")
                a.image_duration = val
            elif k == "crf":
                val = int(v)
                if val < 0 or val > 51:
                    raise ValueError("crf 必须在 0 到 51 之间")
                a.crf = val
            elif k == "max_resolution":
                a.max_resolution = durations.parse_resolution(v) if isinstance(v, str) else tuple(v)
            elif k == "fps":
                val = int(v)
                if val <= 0:
                    raise ValueError("fps 必须大于 0")
                a.fps = val
            elif k == "canvas_mode":
                if v not in ("max", "first"):
                    raise ValueError("canvas_mode 必须是 'max' 或 'first'")
                a.canvas_mode = v
            elif k == "x264_preset":
                if v not in (
                    "ultrafast", "superfast", "veryfast", "faster", "fast",
                    "medium", "slow", "slower", "veryslow",
                ):
                    raise ValueError(f"无效的 x264_preset: {v}")
                a.x264_preset = v
            elif k == "ken_burns":
                a.ken_burns = bool(v)
            elif k == "keep_segments":
                val = int(v)
                if val < 0:
                    raise ValueError("keep_segments 不能为负数")
                a.keep_segments = val
            elif k == "no_upload":
                a.no_upload = bool(v)
            elif k == "force_scan":
                a.force_scan = bool(v)
            elif k == "no_scan":
                pass
            elif k == "privacy":
                if v not in ("private", "unlisted"):
                    raise ValueError("privacy 必须是 'private' 或 'unlisted'")
                a.privacy = v
            elif k == "playlist":
                a.playlist = str(v).strip() or None
            elif k == "only_camera_photos":
                a.only_camera_photos = bool(v)
            elif k == "only_phone_videos":
                a.only_phone_videos = bool(v)
            elif k in ("quiet_period", "poll_interval", "mtime_interval", "quota_backoff"):
                val = durations.parse_duration(v) if isinstance(v, str) else float(v)
                setattr(a, k, val)
            elif k == "short_video_threshold":
                val = durations.parse_duration(v) if isinstance(v, str) else float(v)
                if val < 0:
                    raise ValueError("short_video_threshold 不能为负数")
                a.short_video_threshold = val
            elif k == "min_video_duration":
                val = durations.parse_duration(v) if isinstance(v, str) else float(v)
                if val < 0:
                    raise ValueError("min_video_duration 不能为负数")
                a.min_video_duration = val
            elif k == "ignore_live_photos":
                a.ignore_live_photos = bool(v)
            elif k == "encoder":
                a.encoder = str(v).strip().lower()
            elif k == "watch":
                a.watch = bool(v)
            elif k == "force_plan":
                a.force_plan = bool(v)
            elif k == "input":
                if v and str(v).strip():
                    a.input = os.path.abspath(str(v).strip())
            elif k == "db":
                if v and str(v).strip():
                    a.db = os.path.abspath(str(v).strip())
                    self._db_path = a.db
            elif k == "log_file":
                if v and str(v).strip():
                    a.log_file = os.path.abspath(str(v).strip())
            elif k == "timezone":
                if v:
                    a.timezone = durations.parse_timezone(str(v).strip())

    def is_task_running(self) -> bool:
        return self.current_task is not None

    def segment_params(self) -> tuple:
        a = self.args
        return (
            a.image_duration,
            a.crf,
            a.max_resolution,
            a.ken_burns,
            a.canvas_mode,
            a.fps,
            a.x264_preset,
            getattr(a, "short_video_threshold", 300.0),
            getattr(a, "ignore_live_photos", True),
            getattr(a, "min_video_duration", 2.0),
        )

    def find_local_segment_file(self, segment_id: str, title: str | None = None) -> str | None:
        """Find the local .mp4 file path for a segment if it exists in uploaded_segments/."""
        if not self.db:
            return None
        srec = self.db.segments.get(segment_id, {})
        t = title or srec.get("title")
        candidates = []
        if t:
            candidates.append(os.path.join(self.segments_dir, f"{t}.mp4"))
            for ext in VIDEO_EXTENSIONS:
                candidates.append(os.path.join(self.segments_dir, f"{t}{ext}"))
        candidates.append(os.path.join(self.segments_dir, f"{segment_id}.mp4"))
        for ext in VIDEO_EXTENSIONS:
            candidates.append(os.path.join(self.segments_dir, f"{segment_id}{ext}"))

        # For historical databases that stored output_path, also check its basename in segments_dir
        old_out = srec.get("output_path")
        if old_out:
            candidates.append(os.path.join(self.segments_dir, os.path.basename(old_out)))
            if os.path.isfile(old_out) and os.path.getsize(old_out) > 0:
                return old_out

        for c in candidates:
            if os.path.isfile(c) and os.path.getsize(c) > 0:
                return c

        # Search for sid[:16] prefix/suffix in uploaded_segments directory
        if os.path.isdir(self.segments_dir):
            sid16 = segment_id[:16]
            valid_exts = VIDEO_EXTENSIONS | {".mp4"}
            for entry in os.scandir(self.segments_dir):
                if entry.is_file() and os.path.splitext(entry.name)[1].lower() in valid_exts and sid16 in entry.name:
                    if entry.stat().st_size > 0:
                        return entry.path

        return None

    def delete_local_segment_file(self, segment_id: str) -> tuple[bool, str]:
        """Delete local segment video file from disk to free storage space."""
        if not self.db:
            return False, "数据库未初始化"
        srec = self.db.segments.get(segment_id)
        if not srec:
            return False, f"未找到分段: {segment_id}"

        title = srec.get("title") or segment_id[:16]
        local_path = self.find_local_segment_file(segment_id, title)
        if not local_path or not os.path.isfile(local_path):
            return False, "本地未找到该分段的视频文件，无需删除"

        try:
            os.remove(local_path)
            _logger.info("deleted local segment video file: %s", local_path)
            # If not yet uploaded to YouTube, reset status back to pending
            if not srec.get("youtube_video_id"):
                srec["status"] = "pending"
                self.db.upsert_segment(segment_id, srec)
                self.db.save()
            return True, f"已成功删除本地视频文件 ({os.path.basename(local_path)})"
        except OSError as exc:
            _logger.error("failed to delete local segment file %s: %s", local_path, exc)
            return False, f"删除本地文件失败: {exc}"

    def ensure_uploader(self):
        """Lazy-initialize YouTube uploader if credentials are present."""
        if self.uploader is not None:
            return self.uploader
        from . import cli

        self.uploader = cli._build_uploader(self.args, self.db)
        return self.uploader

    # ------------------------------------------------------------- Async Actions

    def trigger_scan(self) -> tuple[bool, str]:
        """Trigger a full filesystem scan and pipeline run in a background thread."""
        if not self.task_lock.acquire(blocking=False):
            return False, f"已有任务正在运行中 ({self.current_task})"

        self.current_task = "扫描与构建"

        def _worker():
            from . import cli, web

            try:
                _logger.info("manual full scan triggered from dashboard")
                web.set_web_status("scanning", "正在全量扫描媒体文件...")
                cli.run_pipeline(self.args, force_scan=True)
            except Exception as exc:
                _logger.error("scan pipeline failed: %s", exc, exc_info=True)
                web.set_web_status("error", f"扫描任务出错: {exc}")
            finally:
                self.current_task = None
                self.task_lock.release()

        t = threading.Thread(target=_worker, name="ManualScanWorker", daemon=True)
        t.start()
        return True, "全量扫描任务已在后台启动"

    def rebuild_segment(self, segment_id: str) -> tuple[bool, str]:
        """Rebuild a single segment from its component files."""
        if not self.db:
            return False, "数据库未初始化"
        srec = self.db.segments.get(segment_id)
        if not srec:
            from . import web

            collected = web._collect_segments()
            if segment_id in collected:
                srec = collected[segment_id]

        if not srec:
            return False, f"未找到分段: {segment_id}"

        if not self.task_lock.acquire(blocking=False):
            return False, f"已有任务正在运行中 ({self.current_task})"

        title = srec.get("title") or segment_id[:16]
        self.current_task = f"重新构建 {title}"

        def _worker():
            from . import web

            try:
                _logger.info("manual rebuild triggered for segment %s (%s)", segment_id, title)
                web.set_web_status("rebuilding", f"正在重新构建 {title}")

                # Collect files
                files = []
                excluded_ids = set(srec.get("excluded_file_ids", []))
                for fid in srec.get("file_ids", []):
                    if fid in excluded_ids:
                        continue
                    frec = self.db.files.get(fid)
                    if frec:
                        files.append(ScannedFile.from_record(fid, frec, self.args.input))
                    else:
                        _logger.warning("file %s not found in db index", fid)

                if not files:
                    raise RuntimeError(f"分段 {segment_id} 的关联源文件在数据库中不存在")

                config = TranscodeConfig(
                    crf=self.args.crf,
                    max_resolution=self.args.max_resolution,
                    canvas_mode=self.args.canvas_mode,
                    ken_burns=self.args.ken_burns,
                    image_duration=self.args.image_duration,
                    fps=self.args.fps,
                    preset=self.args.x264_preset,
                    encoder=getattr(self.args, "encoder", "auto"),
                )

                is_single_vid = len(files) == 1 and files[0].type == FILE_TYPE_VIDEO
                if is_single_vid:
                    ext = os.path.splitext(files[0].abs_path)[1].lower() or ".mp4"
                    out_path = os.path.join(self.segments_dir, f"{title}{ext}")
                else:
                    out_path = os.path.join(self.segments_dir, f"{title}.mp4")

                def transcode_fn(flist, _out=out_path):
                    def progress(
                        done: int,
                        total: int,
                        item,
                        is_resumed: bool = False,
                        file_progress: float | None = None,
                        fps: float | None = None,
                        speed: str | None = None,
                    ) -> None:
                        web.update_web_transcode(
                            segment_id=segment_id,
                            title=title,
                            done=done,
                            total=total,
                            current_file=item.rel_path,
                            resumed=is_resumed,
                            file_progress=file_progress,
                            fps=fps,
                            speed=speed,
                        )

                    return transcode_segment(flist, _out, config, progress=progress)

                # Execute transcode
                _, chapters = transcode_fn(files)

                # Check upload
                uploader = None if getattr(self.args, "no_upload", False) else self.ensure_uploader()
                new_video_id = None

                if uploader:
                    web.set_web_status("uploading", f"正在上传 {title}")
                    file_size = os.path.getsize(out_path) if os.path.isfile(out_path) else 0
                    web.update_web_upload(segment_id, title=title, percent=0.0, uploaded_bytes=0, total_bytes=file_size)
                    start_time = time.monotonic()

                    def _rebuild_up_cb(pct: float, uploaded_b: int = 0, total_b: int = 0) -> None:
                        pct_val = pct * 100.0
                        now = time.monotonic()
                        elapsed = max(0.001, now - start_time)
                        speed_bps = uploaded_b / elapsed if uploaded_b > 0 else 0
                        speed_str = f"{speed_bps / (1024 * 1024):.1f} MB/s" if speed_bps > 0 else "N/A"
                        web.update_web_upload(
                            segment_id,
                            title=title,
                            percent=pct_val,
                            uploaded_bytes=uploaded_b,
                            total_bytes=total_b or file_size,
                            speed=speed_str,
                        )
                        web.set_web_status("uploading", f"正在上传 {title} ({pct_val:.1f}%) · {speed_str}")

                    old_vid = srec.get("youtube_video_id")
                    desc = build_segment_description(files, total_duration=srec.get("duration_seconds"))
                    new_video_id = uploader.upload(out_path, title, desc, privacy=self.args.privacy, progress_callback=_rebuild_up_cb)
                    uploader.verify(new_video_id)
                    if old_vid:
                        try:
                            uploader.delete_video(old_vid)
                        except Exception as del_exc:
                            _logger.warning("could not delete previous video %s: %s", old_vid, del_exc)
                    prev = list(srec.get("previous_video_ids", []))
                    if old_vid and old_vid not in prev:
                        prev.append(old_vid)
                    srec["previous_video_ids"] = prev

                saved_out = out_path if self.args.keep_segments > 0 and os.path.exists(out_path) else None
                srec["output_path"] = saved_out
                srec["youtube_video_id"] = new_video_id
                srec["status"] = SEGMENT_STATUS_SEALED
                srec["chapters"] = chapters
                srec["duration_seconds"] = sum(f.duration_seconds or 0.0 for f in files)
                srec["error"] = None
                self.db.upsert_segment(segment_id, srec)
                self.db.save()

                rotate_uploaded_segments(self.segments_dir, self.args.keep_segments)
                web.finish_web_segment(segment_id, youtube_video_id=new_video_id)
                web.set_web_status("idle", f"分段 {title} 重建完成")
                _logger.info("segment %s rebuilt successfully (video_id=%s)", segment_id, new_video_id)
            except Exception as exc:
                _logger.error("segment %s rebuild failed: %s", segment_id, exc, exc_info=True)
                srec["status"] = SEGMENT_STATUS_FAILED
                srec["error"] = str(exc)
                self.db.upsert_segment(segment_id, srec)
                self.db.save()
                web.set_web_status("error", f"重建失败: {exc}")
            finally:
                web.clear_web_upload()
                self.current_task = None
                self.task_lock.release()

        t = threading.Thread(target=_worker, name=f"Rebuild-{segment_id[:8]}", daemon=True)
        t.start()
        return True, f"分段 {title} 重新构建任务已在后台启动"

    def trigger_rebuild(self, segment_id: str) -> tuple[bool, str]:
        """Trigger rebuilding a single segment from its component files (alias for rebuild_segment)."""
        return self.rebuild_segment(segment_id)

    def upload_segment(self, segment_id: str) -> tuple[bool, str]:
        """Upload a locally existing segment video file to YouTube."""
        if not self.db:
            return False, "数据库未初始化"
        srec = self.db.segments.get(segment_id)
        if not srec:
            return False, f"未找到分段: {segment_id}"

        title = srec.get("title") or segment_id[:16]
        local_path = self.find_local_segment_file(segment_id, title)
        if not local_path or not os.path.isfile(local_path):
            return False, "本地 uploaded_segments/ 中未找到该视频文件，请先点击【重新构建】"

        uploader = self.ensure_uploader()
        if not uploader:
            return False, "YouTube 凭据不可用，请先在控制台完成授权"

        if not self.task_lock.acquire(blocking=False):
            return False, f"已有任务正在运行中 ({self.current_task})"

        self.current_task = f"上传 {title}"

        def _worker():
            from . import web

            try:
                _logger.info("manual upload triggered for segment %s (%s)", segment_id, title)
                file_size = os.path.getsize(local_path) if os.path.isfile(local_path) else 0
                web.update_web_upload(segment_id, title=title, percent=0.0, uploaded_bytes=0, total_bytes=file_size)
                web.set_web_status("uploading", f"正在上传 {title} (0.0%)")

                start_time = time.monotonic()

                def _upload_cb(pct: float, uploaded_b: int = 0, total_b: int = 0) -> None:
                    pct_val = pct * 100.0
                    now = time.monotonic()
                    elapsed = max(0.001, now - start_time)
                    speed_bps = uploaded_b / elapsed if uploaded_b > 0 else 0
                    speed_str = f"{speed_bps / (1024 * 1024):.1f} MB/s" if speed_bps > 0 else "N/A"
                    web.update_web_upload(
                        segment_id,
                        title=title,
                        percent=pct_val,
                        uploaded_bytes=uploaded_b,
                        total_bytes=total_b or file_size,
                        speed=speed_str,
                    )
                    web.set_web_status("uploading", f"正在上传 {title} ({pct_val:.1f}%) · {speed_str}")

                file_ids = srec.get("file_ids", [])
                files = [
                    ScannedFile.from_record(fid, self.db.files[fid], self.args.input)
                    for fid in file_ids if fid in self.db.files
                ]
                desc = build_segment_description(files, total_duration=srec.get("duration_seconds"))
                video_id = uploader.upload(local_path, title, desc, privacy=self.args.privacy, progress_callback=_upload_cb)
                uploader.verify(video_id)

                srec["youtube_video_id"] = video_id
                srec["status"] = SEGMENT_STATUS_SEALED
                if getattr(self.args, "keep_segments", 0) <= 0:
                    srec["output_path"] = None
                self.db.upsert_segment(segment_id, srec)
                self.db.save()

                rotate_uploaded_segments(self.segments_dir, self.args.keep_segments)
                web.finish_web_segment(segment_id, youtube_video_id=video_id)
                web.set_web_status("idle", f"分段 {title} 上传完成: {video_id}")
                _logger.info("segment %s uploaded successfully -> %s", segment_id, video_id)
            except Exception as exc:
                _logger.error("segment %s upload failed: %s", segment_id, exc, exc_info=True)
                web.set_web_status("error", f"上传失败: {exc}")
            finally:
                web.clear_web_upload()
                self.current_task = None
                self.task_lock.release()

        t = threading.Thread(target=_worker, name=f"Upload-{segment_id[:8]}", daemon=True)
        t.start()
        return True, f"分段 {title} 上传任务已在后台启动"

    def delete_youtube_video(self, segment_id: str) -> tuple[bool, str]:
        """Delete YouTube video for segment and update local database record."""
        if not self.db:
            return False, "数据库未初始化"
        srec = self.db.segments.get(segment_id)
        if not srec:
            return False, f"未找到分段: {segment_id}"

        video_id = srec.get("youtube_video_id")
        if not video_id:
            return False, "该分段没有关联的 YouTube 视频"

        uploader = self.ensure_uploader()
        if not uploader:
            return False, "无法获取 YouTube 凭据以执行删除操作"

        try:
            _logger.info("deleting YouTube video %s for segment %s", video_id, segment_id)
            uploader.delete_video(video_id)

            prev = list(srec.get("previous_video_ids", []))
            if video_id not in prev:
                prev.append(video_id)
            srec["previous_video_ids"] = prev
            srec["youtube_video_id"] = None
            self.db.upsert_segment(segment_id, srec)
            self.db.save()
            _logger.info("YouTube video %s deleted and db updated", video_id)
            return True, f"已从 YouTube 成功删除视频 ({video_id})"
        except Exception as exc:
            _logger.error("delete YouTube video %s failed: %s", video_id, exc, exc_info=True)
            return False, f"YouTube 视频删除失败: {exc}"

    def sync_youtube(self) -> tuple[bool, str, dict[str, int]]:
        """Perform two-way reconciliation with YouTube channel uploads."""
        if not self.db:
            return False, "数据库未初始化", {}
        uploader = self.ensure_uploader()
        if not uploader:
            return False, "YouTube 凭据不可用，请先在控制台完成授权", {}

        if not self.task_lock.acquire(blocking=False):
            return False, f"已有任务正在运行中 ({self.current_task})", {}

        self.current_task = "与 YouTube 同步"
        try:
            from . import web

            _logger.info("manual YouTube sync triggered from dashboard")
            web.set_web_status("syncing", "正在与 YouTube 同步已上传视频列表...")
            remote_index, remote_video_ids, success = fetch_remote_catalog(uploader.service)
            if not success and not remote_index:
                web.set_web_status("error", "无法连接 YouTube 获取视频列表")
                return False, "无法从 YouTube 获取视频列表，请检查网络或授权", {}

            planned = web._server_state.get("planned_segments", [])
            vid_ids_for_check = remote_video_ids if success else None
            stats = reconcile_database(self.db, remote_index, vid_ids_for_check, planned_segments=planned)
            m = stats.get("matched", 0)
            d = stats.get("deleted", 0)
            msg = f"与 YouTube 同步完成（匹配到 {m} 个已上传分片，检测到 {d} 个云端已删除分片）"
            _logger.info(msg)
            web.set_web_status("idle", msg)
            return True, msg, stats
        except Exception as exc:
            _logger.error("YouTube sync failed: %s", exc, exc_info=True)
            try:
                from . import web
                web.set_web_status("error", f"与 YouTube 同步失败: {exc}")
            except Exception:
                pass
            return False, f"与 YouTube 同步失败: {exc}", {}
        finally:
            self.current_task = None
            self.task_lock.release()

    def get_media_stats(self) -> dict:
        """Compute summary statistics across all scanned media files and database."""
        if not self.db:
            return {
                "total_files": 0,
                "total_size_bytes": 0,
                "total_size_formatted": "0 B",
                "photos_count": 0,
                "videos_count": 0,
                "skipped_count": 0,
                "earliest_date": None,
                "latest_date": None,
                "date_span": "暂无数据",
                "total_segments": 0,
                "uploaded_segments": 0,
            }

        files = self.db.files
        total_files = len(files)
        total_size = 0
        photos_count = 0
        videos_count = 0
        dates: list[str] = []

        for frec in files.values():
            sz = frec.get("size_bytes", 0) or 0
            total_size += sz
            ftype = frec.get("type", "")
            if ftype == FILE_TYPE_VIDEO:
                videos_count += 1
            else:
                photos_count += 1
            cap = frec.get("captured_at_utc")
            if cap:
                dates.append(str(cap)[:10])

        dates.sort()
        earliest = dates[0] if dates else None
        latest = dates[-1] if dates else None
        date_span = f"{earliest} ~ {latest}" if (earliest and latest) else "暂无数据"

        if total_size >= 1024 * 1024 * 1024:
            sz_str = f"{total_size / (1024 * 1024 * 1024):.2f} GB"
        elif total_size >= 1024 * 1024:
            sz_str = f"{total_size / (1024 * 1024):.1f} MB"
        elif total_size >= 1024:
            sz_str = f"{total_size / 1024:.0f} KB"
        else:
            sz_str = f"{total_size} B"

        skipped_count = len(getattr(self.db, "skipped_files", {}))
        total_segments = len(self.db.segments)
        uploaded_segments = sum(1 for s in self.db.segments.values() if s.get("youtube_video_id"))

        return {
            "total_files": total_files,
            "total_size_bytes": total_size,
            "total_size_formatted": sz_str,
            "photos_count": photos_count,
            "videos_count": videos_count,
            "skipped_count": skipped_count,
            "earliest_date": earliest,
            "latest_date": latest,
            "date_span": date_span,
            "total_segments": total_segments,
            "uploaded_segments": uploaded_segments,
        }

    def get_segments_view_data(self, include_files: bool = True) -> list[dict]:
        """Return rich segments list for Segments management page.

        If include_files is False, constituent files and candidates are omitted,
        drastically reducing serialization and network overhead for large libraries.
        Detailed files can be fetched on demand per segment via get_segment_files_data().
        """
        if not self.db:
            return []

        # If database has files but no segments planned yet, auto-plan them
        if not self.db.segments and self.db.files:
            try:
                from . import web
                collected = web._collect_segments()
                for sid, srec in collected.items():
                    if sid not in self.db.segments:
                        self.db.upsert_segment(sid, {
                            "title": srec.get("title") or sid[:16],
                            "file_ids": srec.get("file_ids", []),
                            "range": srec.get("range", ["", ""]),
                            "duration_seconds": srec.get("duration_seconds", 0.0),
                            "youtube_video_id": srec.get("youtube_video_id"),
                            "status": srec.get("status", "pending"),
                            "chapters": [],
                            "last_rebuilt_at": None,
                            "attempts": 0,
                            "error": None,
                        })
                if collected:
                    self.db.save()
            except Exception as exc:
                _logger.warning("could not auto-plan segments in get_segments_view_data: %s", exc)

        unassigned_files = []
        unassigned_keys = []
        if include_files:
            # 1. Collect all currently assigned file_ids
            assigned_file_ids = set()
            for srec in self.db.segments.values():
                for fid in srec.get("file_ids", []):
                    assigned_file_ids.add(fid)

            # 2. Collect candidate unassigned files
            for fid, frec in self.db.files.items():
                if fid not in assigned_file_ids:
                    cap = frec.get("captured_at_utc", "")
                    if cap:
                        unassigned_files.append((str(cap)[:19], fid, frec))

            unassigned_files.sort(key=lambda x: x[0])
            unassigned_keys = [x[0] for x in unassigned_files]

        res = []
        for sid, srec in self.db.segments.items():
            title = srec.get("title") or sid[:16]
            status = srec.get("status", "pending")
            dur = srec.get("duration_seconds", 0.0)
            rng = srec.get("range") or ["", ""]
            start_ts = rng[0] if len(rng) > 0 else ""
            end_ts = rng[1] if len(rng) > 1 else ""
            vid = srec.get("youtube_video_id")
            out_p = srec.get("output_path")
            fids = srec.get("file_ids", [])
            excluded_ids = set(srec.get("excluded_file_ids", []))

            # Constituent files details (only if requested)
            files = []
            candidates = []
            if include_files:
                for fid in fids:
                    frec = self.db.files.get(fid)
                    if frec:
                        files.append({
                            "file_id": fid,
                            "path": frec.get("path", ""),
                            "name": frec.get("name") or os.path.basename(frec.get("path", "")),
                            "type": frec.get("type", "image"),
                            "duration_seconds": frec.get("duration_seconds", 3.0),
                            "resolution": frec.get("resolution", ""),
                            "size_bytes": frec.get("size_bytes", 0),
                            "captured_at_utc": frec.get("captured_at_utc", ""),
                            "is_excluded": (fid in excluded_ids),
                        })

                # Check for candidate files falling into this segment's [start_ts, end_ts]
                if start_ts and end_ts:
                    s_bound = str(start_ts)[:19]
                    e_bound = str(end_ts)[:19]
                    left = bisect.bisect_left(unassigned_keys, s_bound)
                    right = bisect.bisect_right(unassigned_keys, e_bound)
                    for _, fid, frec in unassigned_files[left:right]:
                        candidates.append({
                            "file_id": fid,
                            "path": frec.get("path", ""),
                            "name": frec.get("name") or os.path.basename(frec.get("path", "")),
                            "type": frec.get("type", "image"),
                            "duration_seconds": frec.get("duration_seconds", 3.0),
                            "resolution": frec.get("resolution", ""),
                            "size_bytes": frec.get("size_bytes", 0),
                            "captured_at_utc": frec.get("captured_at_utc", ""),
                        })

            local_path = self.find_local_segment_file(sid, title)
            has_local = bool(local_path and os.path.isfile(local_path))
            local_size = os.path.getsize(local_path) if has_local else 0
            local_size_formatted = f"{local_size / (1024 * 1024):.1f} MB" if has_local else None

            active_dur = sum(f["duration_seconds"] for f in files if not f["is_excluded"]) if files else (dur or 0.0)
            cfg_dur = float(getattr(self.args, "segment_duration", 1200.0))
            is_oversized = (dur or active_dur) > cfg_dur

            from . import web
            upload_state = web._server_state.get("upload", {})
            transcode_state = web._server_state.get("transcode", {})
            upload_q = web.get_upload_queue()

            upload_progress = None
            if upload_state.get("segment_id") == sid:
                upload_progress = {
                    "percent": upload_state.get("percent", 0.0),
                    "speed": upload_state.get("speed", ""),
                    "uploaded_bytes": upload_state.get("uploaded_bytes", 0),
                    "total_bytes": upload_state.get("total_bytes", 0),
                }

            transcode_progress = None
            if transcode_state.get("segment_id") == sid:
                transcode_progress = {
                    "percent": transcode_state.get("percent", 0.0),
                    "current_file": transcode_state.get("current_file", ""),
                    "total_files": transcode_state.get("total_files", 0),
                    "done_files": transcode_state.get("done_files", 0),
                }

            is_upload_queued = (sid in upload_q and sid != upload_state.get("segment_id"))

            res.append({
                "segment_id": sid,
                "title": title,
                "status": status,
                "duration_seconds": dur or active_dur,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "youtube_video_id": vid,
                "local_path": local_path,
                "has_local_file": has_local,
                "local_size_bytes": local_size,
                "local_size_formatted": local_size_formatted,
                "total_files": len(fids),
                "file_count": len(fids),
                "active_files_count": len([f for f in fids if f not in excluded_ids]),
                "files": files,
                "candidate_files": candidates,
                "is_oversized": is_oversized,
                "upload_progress": upload_progress,
                "transcode_progress": transcode_progress,
                "is_upload_queued": is_upload_queued,
            })

        # Sort segments chronologically
        res.sort(key=lambda s: (s["start_ts"] or "", s["title"]))
        return res

    def get_segment_files_data(self, segment_id: str) -> dict | None:
        """Return detailed constituent files and candidate files for a single segment."""
        if not self.db:
            return None
        srec = self.db.segments.get(segment_id)
        if not srec:
            for s, r in self.db.segments.items():
                if s.startswith(segment_id) or (len(segment_id) >= 8 and s[:len(segment_id)].lower() == segment_id.lower()):
                    srec = r
                    segment_id = s
                    break
        if not srec:
            return None

        title = srec.get("title") or segment_id[:16]
        fids = srec.get("file_ids", [])
        excluded_ids = set(srec.get("excluded_file_ids", []))
        media_dir = getattr(self.args, "input", None) or "."

        files = []
        items = []
        for fid in fids:
            frec = self.db.files.get(fid)
            if not frec:
                continue
            rel_path = frec.get("path", "")
            abs_path = os.path.abspath(os.path.join(media_dir, rel_path))
            f_type = frec.get("type", "image")
            dur = float(frec.get("duration_seconds") or (0.0 if f_type == "video" else 3.0))
            sz = int(frec.get("size_bytes") or 0)
            res_str = frec.get("resolution", "")
            cap_utc = frec.get("captured_at_utc", "")
            name = frec.get("name") or os.path.basename(rel_path)

            file_entry = {
                "id": fid,
                "file_id": fid,
                "path": rel_path,
                "abs_path": abs_path,
                "name": name,
                "type": f_type,
                "duration": dur,
                "duration_seconds": dur,
                "resolution": res_str,
                "size": sz,
                "size_bytes": sz,
                "captured_at": cap_utc,
                "captured_at_utc": cap_utc,
                "is_excluded": (fid in excluded_ids),
            }
            files.append(file_entry)
            items.append(file_entry)

        # Candidates within [start_ts, end_ts]
        candidates = []
        rng = srec.get("range") or ["", ""]
        start_ts = rng[0] if len(rng) > 0 else ""
        end_ts = rng[1] if len(rng) > 1 else ""
        if start_ts and end_ts:
            s_bound = str(start_ts)[:19]
            e_bound = str(end_ts)[:19]
            assigned_file_ids = set()
            for seg in self.db.segments.values():
                for afid in seg.get("file_ids", []):
                    assigned_file_ids.add(afid)

            for fid, frec in self.db.files.items():
                if fid in assigned_file_ids:
                    continue
                cap = str(frec.get("captured_at_utc", ""))[:19]
                if s_bound <= cap <= e_bound:
                    rel_p = frec.get("path", "")
                    candidates.append({
                        "file_id": fid,
                        "path": rel_p,
                        "name": frec.get("name") or os.path.basename(rel_p),
                        "type": frec.get("type", "image"),
                        "duration_seconds": float(frec.get("duration_seconds") or 3.0),
                        "resolution": frec.get("resolution", ""),
                        "size_bytes": int(frec.get("size_bytes") or 0),
                        "captured_at_utc": frec.get("captured_at_utc", ""),
                    })

        active_dur = sum(f["duration_seconds"] for f in files if not f["is_excluded"])
        return {
            "ok": True,
            "segment_id": segment_id,
            "title": title,
            "status": srec.get("status") or "pending",
            "duration_seconds": float(srec.get("duration_seconds") or active_dur),
            "youtube_video_id": srec.get("youtube_video_id"),
            "items": items,
            "files": files,
            "candidate_files": candidates,
            "active_files_count": len([f for f in files if not f["is_excluded"]]),
            "total_files": len(files),
        }

    def toggle_segment_file(self, segment_id: str, file_id: str, is_excluded: bool) -> tuple[bool, str]:
        """Toggle inclusion of a specific file in a segment."""
        if not self.db:
            return False, "数据库未初始化"
        srec = self.db.segments.get(segment_id)
        if not srec:
            return False, f"未找到分段: {segment_id}"

        excluded = list(srec.get("excluded_file_ids", []))
        if is_excluded:
            if file_id not in excluded:
                excluded.append(file_id)
        else:
            if file_id in excluded:
                excluded.remove(file_id)

        srec["excluded_file_ids"] = excluded
        # If output was already transcoded, mark as pending rebuild
        if srec.get("output_path") and not srec.get("youtube_video_id"):
            srec["status"] = "pending"
        self.db.upsert_segment(segment_id, srec)
        self.db.save()
        return True, "文件构建状态已更新"

    def include_candidate_file(self, segment_id: str, file_id: str) -> tuple[bool, str]:
        """Include a candidate unassigned file into an existing segment."""
        if not self.db:
            return False, "数据库未初始化"
        srec = self.db.segments.get(segment_id)
        if not srec:
            return False, f"未找到分段: {segment_id}"

        fids = list(srec.get("file_ids", []))
        if file_id not in fids:
            fids.append(file_id)
            srec["file_ids"] = fids
            excluded = list(srec.get("excluded_file_ids", []))
            if file_id in excluded:
                excluded.remove(file_id)
                srec["excluded_file_ids"] = excluded
            srec["status"] = "pending"
            self.db.upsert_segment(segment_id, srec)
            self.db.save()
            return True, f"已将文件纳入分段 {srec.get('title', segment_id[:12])}"
        return True, "文件已在分段中"

    def split_segment(self, segment_id: str) -> tuple[bool, str, list[str]]:
        """Split an oversized segment into 2 or more segments respecting segment_duration."""
        if not self.db:
            return False, "数据库未初始化", []
        srec = self.db.segments.get(segment_id)
        if not srec:
            return False, f"未找到分段: {segment_id}", []

        fids = srec.get("file_ids", [])
        files = [
            ScannedFile.from_record(fid, self.db.files[fid], self.args.input)
            for fid in fids if fid in self.db.files
        ]
        if not files:
            return False, "分段内无可用媒体文件", []

        seg_dur = float(getattr(self.args, "segment_duration", 1200.0))
        params = self.segment_params()
        short_th = getattr(self.args, "short_video_threshold", 300.0)
        ign_lp = getattr(self.args, "ignore_live_photos", True)
        min_vd = getattr(self.args, "min_video_duration", 2.0)

        groups = greedy_pack(
            sorted(files, key=lambda f: f.captured_epoch or 0.0),
            seg_dur,
            short_video_threshold=short_th,
            ignore_live_photos=ign_lp,
            min_video_duration=min_vd,
        )
        if len(groups) <= 1:
            return False, "该分段文件总时长未超过分段时长上限，无需拆分", [segment_id]

        new_segment_ids = []
        old_vid = srec.get("youtube_video_id")
        old_prev = list(srec.get("previous_video_ids", []))
        self.db.segments.pop(segment_id, None)

        for i, group in enumerate(groups, start=1):
            seg = _make_segment(group, params)
            sid = seg.segment_id
            new_segment_ids.append(sid)
            vid = old_vid if (i == 1 and old_vid) else None
            self.db.upsert_segment(
                sid,
                {
                    "title": seg.title,
                    "file_ids": seg.file_ids,
                    "range": [seg.start_ts, seg.end_ts],
                    "duration_seconds": seg.duration_seconds,
                    "youtube_video_id": vid,
                    "previous_video_ids": old_prev if i == 1 else [],
                    "status": "pending" if not vid else SEGMENT_STATUS_SEALED,
                    "chapters": [],
                    "last_rebuilt_at": None,
                    "attempts": 0,
                    "error": None,
                }
            )

        self.db.save()
        msg = f"已将分段拆分为 {len(new_segment_ids)} 个符合时长限制的新分段"
        _logger.info("split segment %s into %d segments: %s", segment_id, len(new_segment_ids), new_segment_ids)
        return True, msg, new_segment_ids

    def force_replan(self) -> tuple[bool, str]:
        """Force re-planning all segments from scratch."""
        if not self.db:
            return False, "数据库未初始化"
        if not self.task_lock.acquire(blocking=False):
            return False, f"已有任务正在运行中 ({self.current_task})"

        self.current_task = "强制重新规划分片"

        def _worker():
            from . import cli, web

            try:
                web.set_web_status("scanning", "正在强制重新规划分段...")
                self.args.force_plan = True
                self.args.flush = True
                cli.run_pipeline(self.args, force_scan=False)
                web.set_web_status("idle", "分段重新规划完成")
            except Exception as exc:
                _logger.error("force_replan failed: %s", exc, exc_info=True)
                web.set_web_status("error", f"重新规划出错: {exc}")
            finally:
                self.current_task = None
                self.task_lock.release()

        threading.Thread(target=_worker, daemon=True).start()
        return True, "强制重新规划任务已启动"


