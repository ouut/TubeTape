"""Central coordinator for TubeTape runtime operations.

Manages pipeline runs, background tasks (scan, rebuild, upload, delete),
and dynamic runtime configuration with persistence to config.json.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from . import durations
from .chapters import chapters_text
from .db import SEGMENT_STATUS_FAILED, SEGMENT_STATUS_SEALED, Database
from .planner import ScannedFile, plan
from .rebuild import Rebuilder
from .reconcile import fetch_remote_index
from .scanner import scan
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
])


def rotate_uploaded_segments(directory: str, keep_count: int) -> list[str]:
    """Maintain at most `keep_count` newest .mp4 files in `directory`.

    If keep_count <= 0, deletes all non-hidden .mp4 files.
    Returns list of deleted file paths.
    """
    if not os.path.isdir(directory):
        return []

    mp4_files = []
    for entry in os.scandir(directory):
        if entry.is_file() and entry.name.lower().endswith(".mp4") and not entry.name.startswith("."):
            try:
                mp4_files.append((entry.path, entry.stat().st_mtime))
            except OSError:
                continue

    # Sort descending by mtime (newest first)
    mp4_files.sort(key=lambda x: x[1], reverse=True)

    deleted = []
    to_delete = mp4_files[max(0, keep_count):]
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
        self._config_file = (
            os.path.join(os.path.dirname(args.db), "config.json")
            if getattr(args, "db", None)
            else None
        )
        self.segments_dir = (
            os.path.join(os.path.dirname(args.db), "uploaded_segments")
            if getattr(args, "db", None)
            else "uploaded_segments"
        )
        os.makedirs(self.segments_dir, exist_ok=True)

    def load_saved_config(self) -> dict:
        """Load configuration overrides from config.json if present."""
        if not self._config_file or not os.path.isfile(self._config_file):
            return {}
        try:
            with open(self._config_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self._apply_dict_to_args(saved)
            _logger.info("loaded dynamic config overrides from %s", self._config_file)
            return saved
        except Exception as exc:
            _logger.warning("could not load %s: %s", self._config_file, exc)
            return {}

    def save_config(self) -> None:
        """Persist current configuration to config.json."""
        if not self._config_file:
            return
        try:
            cfg = self.get_config()["config"]
            tmp = self._config_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._config_file)
            _logger.info("saved configuration to %s", self._config_file)
        except Exception as exc:
            _logger.warning("could not save configuration to %s: %s", self._config_file, exc)

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
                "segment_duration": float(getattr(a, "segment_duration", 3600.0)),
                "keep_segments": int(getattr(a, "keep_segments", 0)),
                "no_upload": bool(getattr(a, "no_upload", False)),
                "no_scan": bool(getattr(a, "no_scan", False)),
                "image_duration": float(getattr(a, "image_duration", 3.0)),
                "crf": int(getattr(a, "crf", 16)),
                "max_resolution": max_res_str,
                "canvas_mode": str(getattr(a, "canvas_mode", "max")),
                "fps": int(getattr(a, "fps", 60)),
                "x264_preset": str(getattr(a, "x264_preset", "slow")),
                "ken_burns": bool(getattr(a, "ken_burns", False)),
                "privacy": str(getattr(a, "privacy", "private")),
                "playlist": str(getattr(a, "playlist", "") or ""),
                "only_camera_photos": bool(getattr(a, "only_camera_photos", False)),
                "only_phone_videos": bool(getattr(a, "only_phone_videos", False)),
                "quiet_period": float(getattr(a, "quiet_period", 600.0)),
                "poll_interval": float(getattr(a, "poll_interval", 30.0)),
                "mtime_interval": float(getattr(a, "mtime_interval", 3600.0)),
                "quota_backoff": float(getattr(a, "quota_backoff", 3600.0)),
            },
            "fingerprint_params": list(FINGERPRINT_PARAMS),
        }

    def update_config(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Validate, apply, and persist configuration updates."""
        self._apply_dict_to_args(updates)
        self.save_config()

        # Update web server state
        from . import web

        if "keep_segments" in updates:
            web._server_state["keep_segments"] = self.args.keep_segments
            rotate_uploaded_segments(self.segments_dir, self.args.keep_segments)

        return self.get_config()

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
            elif k == "no_scan":
                a.no_scan = bool(v)
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
        )

    def find_local_segment_file(self, segment_id: str, title: str | None = None) -> str | None:
        """Find the local .mp4 file path for a segment if it exists in uploaded_segments/."""
        if not self.db:
            return None
        srec = self.db.segments.get(segment_id, {})
        if srec.get("output_path") and os.path.isfile(srec["output_path"]):
            return srec["output_path"]

        t = title or srec.get("title")
        candidates = []
        if t:
            candidates.append(os.path.join(self.segments_dir, f"{t}.mp4"))
        candidates.append(os.path.join(self.segments_dir, f"{segment_id}.mp4"))

        for c in candidates:
            if os.path.isfile(c):
                return c

        # Search for sid[:16] prefix/suffix in uploaded_segments directory
        if os.path.isdir(self.segments_dir):
            sid16 = segment_id[:16]
            for entry in os.scandir(self.segments_dir):
                if entry.is_file() and entry.name.lower().endswith(".mp4") and sid16 in entry.name:
                    return entry.path

        return None

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
                cli.run_pipeline(self.args)
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
                for fid in srec.get("file_ids", []):
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
                )

                out_path = os.path.join(self.segments_dir, f"{title}.mp4")

                def transcode_fn(flist, _out=out_path):
                    def progress(done: int, total: int, item) -> None:
                        web.update_web_transcode(
                            segment_id=segment_id,
                            title=title,
                            done=done,
                            total=total,
                            current_file=item.rel_path,
                        )

                    return transcode_segment(flist, _out, config, progress=progress)

                # Execute transcode
                _, chapters = transcode_fn(files)

                # Check upload
                uploader = None if getattr(self.args, "no_upload", False) else self.ensure_uploader()
                new_video_id = None

                if uploader:
                    web.set_web_status("uploading", f"正在上传 {title}")
                    old_vid = srec.get("youtube_video_id")
                    desc = chapters_text(chapters)
                    new_video_id = uploader.upload(out_path, title, desc, privacy=self.args.privacy)
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
                self.current_task = None
                self.task_lock.release()

        t = threading.Thread(target=_worker, name=f"Rebuild-{segment_id[:8]}", daemon=True)
        t.start()
        return True, f"分段 {title} 重新构建任务已在后台启动"

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
                web.set_web_status("uploading", f"正在上传 {title}")
                chapters = srec.get("chapters", [])
                desc = chapters_text(chapters)
                video_id = uploader.upload(local_path, title, desc, privacy=self.args.privacy)
                uploader.verify(video_id)

                srec["youtube_video_id"] = video_id
                srec["status"] = SEGMENT_STATUS_SEALED
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
