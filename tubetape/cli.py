"""Command-line interface for TubeTape.

Parses every startup parameter from prompt.md and runs the pipeline:
scan -> plan -> (dry-run: stop) -> transcode -> upload -> watch.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import auth, durations
from .chapters import build_segment_description, chapters_text
from .db import SEGMENT_STATUS_SEALED, Database
from .coordinator import AppCoordinator
from .log import get_logger, setup_logging
from .planner import Segment, plan
from .rebuild import Rebuilder
from .reconcile import embed_segment_id, fetch_remote_catalog, fetch_remote_index, reconcile_database
from .scanner import FILE_TYPE_VIDEO, ScannedFile, ScanResult, scan
from .transcoder import TranscodeConfig, resolve_encoder, transcode_segment
from .ui import Reporter
from .uploader import QuotaExceededError

_DEFAULT_DB_NAME = "tubetape.json"

# Pipeline exit codes.
_EXIT_OK = 0
_EXIT_ERROR = 1  # fatal (e.g. no credentials)
_EXIT_QUOTA = 2  # stopped early because YouTube quota is exhausted

_log = get_logger("cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tubetape",
        description=(
            "Organize home photos and videos into segmented videos and "
            "upload them to a private YouTube channel."
        ),
    )

    parser.add_argument(
        "--image-duration",
        type=durations.parse_duration,
        default=3.0,
        help="seconds each image plays when converted to video (default: 3)",
    )
    parser.add_argument(
        "--input",
        default=".",
        help="root directory of images and videos (default: current directory)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help=f"JSON database path (default: <input>/{_DEFAULT_DB_NAME})",
    )
    parser.add_argument(
        "-s",
        "--segment-duration",
        type=durations.parse_duration,
        default="20m",
        help="max duration per uploaded video; supports 20m / 1h / 1200s / 1:00:00 "
        "(default: 20m)",
    )
    parser.add_argument(
        "--short-video-threshold",
        type=durations.parse_duration,
        default="300s",
        help="videos shorter than or equal to this threshold are packed into the mixed "
        "media stream alongside photos up to --segment-duration; videos longer than "
        "this threshold are uploaded standalone (default: 300s / 5m; 0 to disable)",
    )
    parser.add_argument(
        "--min-video-duration",
        type=durations.parse_duration,
        default="2s",
        help="minimum video duration to include; videos shorter than this (e.g. accidental "
        "micro-clips) are skipped (default: 2s; 0 to disable)",
    )
    parser.add_argument(
        "--ignore-live-photos",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="skip Apple Live Photo companion videos (<=3.5s videos sharing stem and folder with an image) "
        "(default: True; use --no-ignore-live-photos to disable)",
    )
    parser.add_argument(
        "--timezone",
        type=durations.parse_timezone,
        default="Asia/Shanghai",
        help="timezone used to interpret timezone-less EXIF times "
        "(default: Asia/Shanghai)",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=18,
        help="video re-encode quality; lower is better (default: 18)",
    )
    parser.add_argument(
        "--max-resolution",
        type=durations.parse_resolution,
        default="3840x2160",
        help="resolution ceiling WxH, never upscaling (default: 3840x2160 4K). "
        "The actual output is the segment's bounding box, so <=4K content "
        "stays <=4K and only true 8K content uses 8K",
    )
    parser.add_argument(
        "--canvas-mode",
        choices=["first", "max"],
        default="max",
        help="per-segment canvas resolution: 'max' = bounding box so nothing is "
        "downscaled (default); 'first' = first file's resolution",
    )
    parser.add_argument(
        "--fps",
        type=int,
        default=30,
        help="output frame rate (default: 30; YouTube recommended for static photo albums)",
    )
    parser.add_argument(
        "--x264-preset",
        choices=[
            "ultrafast", "superfast", "veryfast", "faster", "fast",
            "medium", "slow", "slower", "veryslow",
        ],
        default="fast",
        help="x264 speed/efficiency preset; slower = better quality per bitrate "
        "(default: fast)",
    )
    parser.add_argument(
        "--encoder",
        choices=["auto", "libx264", "h264_nvenc", "h264_videotoolbox", "h264_qsv"],
        default="auto",
        help=(
            "video encoder (default: auto). Automatically detects and selects "
            "hardware acceleration: NVIDIA (h264_nvenc) -> Apple (h264_videotoolbox) "
            "-> Intel (h264_qsv) -> CPU (libx264)"
        ),
    )
    parser.add_argument(
        "--ken-burns",
        action="store_true",
        help="enable Ken Burns (pan/zoom) effect on images (default: off)",
    )
    parser.add_argument(
        "--privacy",
        choices=["private", "unlisted"],
        default="private",
        help="YouTube privacy status (default: private)",
    )
    parser.add_argument(
        "--playlist",
        default=None,
        help="YouTube playlist ID to append segments to (in capture-time order)",
    )
    parser.add_argument(
        "--flush",
        action="store_true",
        help="force-seal and upload the pending queue immediately",
    )
    parser.add_argument(
        "--watch",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="watch for file changes (default: on; use --no-watch to disable)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="only scan, compute segments and IDs; no transcode or upload",
    )
    parser.add_argument(
        "--only-camera-photos",
        action="store_true",
        help="only keep camera photos (EXIF Make+Model); skip screenshots/downloads",
    )
    parser.add_argument(
        "--only-phone-videos",
        action="store_true",
        help="only keep phone videos (camera make metadata); skip downloaded/transcoded",
    )
    parser.add_argument(
        "--quiet-period",
        type=durations.parse_duration,
        default="10m",
        help="in watch mode, wait this long after the last change before re-processing (default: 10m)",
    )
    parser.add_argument(
        "--poll-interval",
        type=durations.parse_duration,
        default="30s",
        help="in watch mode, internal tick: how often to re-check time-based "
        "conditions (default: 30s)",
    )
    parser.add_argument(
        "--mtime-interval",
        type=durations.parse_duration,
        default="10m",
        help="in watch mode, interval for the directory-mtime safety-net scan "
        "(catches events watchdog/inotify misses; default: 10m)",
    )
    parser.add_argument(
        "--quota-backoff",
        type=durations.parse_duration,
        default="1h",
        help="when YouTube reports the upload quota is exhausted, wait this long "
        "before retrying (default: 1h)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="more console detail: -v shows INFO, -vv shows DEBUG (default: WARNING+)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help=f"detailed log file path (default: <db>.log, i.e. {_DEFAULT_DB_NAME}.log)",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="run the Google OAuth login flow, save token.json next to --db, and exit",
    )
    parser.add_argument(
        "--client-secret",
        default=None,
        help="path to client_secret.json for --login (default: <db dir>/client_secret.json)",
    )
    parser.add_argument(
        "-p",
        "--port",
        "--web-port",
        dest="web_port",
        type=int,
        default=8080,
        help="port for the web live log and status console (default: 8080, 0 to disable)",
    )
    parser.add_argument(
        "--segments-dir",
        default=None,
        help="directory where transcoded segment videos are written (default: <db dir>/uploaded_segments)",
    )
    parser.add_argument(
        "--keep-segments",
        type=int,
        default=0,
        help="number of latest transcoded segment videos to keep in uploaded_segments/ directory (default: 0 = delete immediately after upload)",
    )
    parser.add_argument(
        "--force-scan",
        action="store_true",
        default=False,
        help="force full scan and rebuild metadata for all files on startup (default: False)",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        default=False,
        help="transcode videos locally without uploading to YouTube (default: False)",
    )
    parser.add_argument(
        "--force-plan",
        action="store_true",
        default=False,
        help="force re-computing all segments from scratch on startup (default: False)",
    )
    return parser



def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)

    args.input = os.path.abspath(args.input)
    if args.db is None:
        args.db = os.path.join(args.input, _DEFAULT_DB_NAME)
    else:
        args.db = os.path.abspath(args.db)

    if args.timezone is None:
        args.timezone = durations.parse_timezone("Asia/Shanghai")

    if getattr(args, "segments_dir", None):
        args.segments_dir = os.path.abspath(args.segments_dir)
    else:
        args.segments_dir = os.path.join(os.path.dirname(args.db), "uploaded_segments")

    if args.crf < 0:
        parser.error("--crf must be >= 0")
    if args.image_duration <= 0:
        parser.error("--image-duration must be > 0")
    if args.segment_duration <= 0:
        parser.error("--segment-duration must be > 0")
    if args.fps <= 0:
        parser.error("--fps must be > 0")
    if args.keep_segments < 0:
        parser.error("--keep-segments must be >= 0")

    suppress_parser = build_parser()
    for action in suppress_parser._actions:
        action.default = argparse.SUPPRESS
    try:
        raw_argv = argv if argv is not None else sys.argv[1:]
        explicit_ns, _ = suppress_parser.parse_known_args(raw_argv)
        args._explicit_cli_args = set(vars(explicit_ns).keys())
    except Exception:
        args._explicit_cli_args = set()

    return args


def _segment_params(args: argparse.Namespace) -> tuple:
    return (
        args.image_duration,
        args.crf,
        args.max_resolution,
        args.ken_burns,
        args.canvas_mode,
        args.fps,
        args.x264_preset,
        getattr(args, "short_video_threshold", 300.0),
        getattr(args, "ignore_live_photos", True),
        getattr(args, "min_video_duration", 2.0),
    )


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
            _log.info("removed old segment video: %s (keeping latest %d)", os.path.basename(path), keep_count)
        except OSError as exc:
            _log.warning("could not delete old segment video %s: %s", path, exc)

    return deleted


def _file_persister(db: Database):
    """Return a scan callback that upserts files and periodically saves the db.

    Saving every N files (or every few seconds) means a long scan can be
    interrupted without losing all of its progress: already-scanned files are
    already persisted with their size+mtime, so the next run reuses the cache.
    """
    state = {"count": 0, "last_save": time.monotonic(), "indexed": 0}

    def persist(item) -> None:
        db.upsert_file(item.file_id, item.to_record())
        state["count"] += 1
        state["indexed"] += 1
        now = time.monotonic()
        if state["count"] >= 1000 or now - state["last_save"] >= 30.0:
            db.save()
            _log.debug("incremental save: %d file(s) indexed so far", len(db.files))
            state["count"] = 0
            state["last_save"] = now

    def flush() -> None:
        if state["count"] > 0:
            db.save()
            _log.debug("flushed %d indexed file(s) to db", state["count"])
            state["count"] = 0
            state["last_save"] = time.monotonic()

    persist.flush = flush  # type: ignore[attr-defined]
    return persist


def _get_matched_coordinator(args: argparse.Namespace):
    from . import web

    coordinator = getattr(args, "_coordinator", None)
    if coordinator is not None:
        return coordinator
    web_coord = web.get_app_coordinator()
    if (
        web_coord
        and getattr(web_coord, "_db_path", None)
        and os.path.abspath(web_coord._db_path) == os.path.abspath(args.db)
    ):
        return web_coord
    return None


def run_pipeline(
    args: argparse.Namespace,
    reporter: Reporter | None = None,
    force_scan: bool = False,
) -> int:
    import threading

    reporter = reporter or Reporter()

    coordinator = _get_matched_coordinator(args)
    if coordinator and coordinator.db is not None and os.path.abspath(coordinator.db.path) == os.path.abspath(args.db):
        db = coordinator.db
    else:
        db = Database.load(args.db)
        if coordinator:
            coordinator.db = db

    if coordinator:
        if coordinator.is_cancelled():
            _log.info("pipeline start aborted: coordinator is cancelled")
            return _EXIT_OK
        coordinator.enter_pipeline(threading.current_thread())

    try:
        return _run_pipeline_impl(args, reporter, force_scan, db)
    except KeyboardInterrupt:
        if not args.dry_run and db is not None:
            try:
                db.save()
                _log.info("interrupted by signal; database saved cleanly to %s", db.path)
            except Exception as e:
                _log.warning("failed to save database on interrupt: %s", e)
        raise
    except Exception as exc:
        if coordinator and coordinator.is_cancelled():
            _log.info("pipeline execution cancelled: %s", exc)
            return _EXIT_OK
        raise
    finally:
        if coordinator:
            coordinator.exit_pipeline(threading.current_thread())


def _run_pipeline_impl(
    args: argparse.Namespace,
    reporter: Reporter,
    force_scan: bool,
    db: Database,
) -> int:
    from . import web

    web.set_web_context(
        media_dir=args.input,
        db=db,
        db_path=args.db,
        keep_segments=getattr(args, "keep_segments", 0),
    )

    _log.info(
        "pipeline start: input=%s db=%s timezone=%s segment_duration=%.1fs "
        "crf=%d max_resolution=%s ken_burns=%s privacy=%s watch=%s dry_run=%s "
        "only_camera_photos=%s only_phone_videos=%s flush=%s keep_segments=%d",
        args.input,
        args.db,
        args.timezone,
        args.segment_duration,
        args.crf,
        f"{args.max_resolution[0]}x{args.max_resolution[1]}",
        args.ken_burns,
        args.privacy,
        args.watch,
        args.dry_run,
        args.only_camera_photos,
        args.only_phone_videos,
        args.flush,
        getattr(args, "keep_segments", 0),
    )
    _log.info("database loaded: %d file(s), %d segment(s)", len(db.files), len(db.segments))

    # If this is a real run (not dry-run and not no-upload), ensure YouTube uploader credentials
    # early so any OAuth authorization prompts or web waits happen up-front.
    coordinator = _get_matched_coordinator(args)
    uploader = None
    if coordinator and coordinator.uploader is not None:
        uploader = coordinator.uploader
    elif not args.dry_run and not getattr(args, "no_upload", False):
        uploader = _build_uploader(args, db)
        if uploader is None:
            reporter.status("no YouTube credentials; set TUBETAPE_TOKEN or token.json")
            _log.error("no YouTube credentials found (TUBETAPE_TOKEN env or token.json next to db)")
            db.save()
            _log.info("database saved to %s", db.path)
            return _EXIT_ERROR

    if coordinator:
        coordinator.db = db
        if uploader is not None:
            coordinator.uploader = uploader

    is_force_scan = force_scan or getattr(args, "force_scan", False)
    web.update_web_scanner(is_scanning=True, count=0, total=0, mode="checking", retained=len(db.files))
    web.set_web_status("scanning", f"正在检查/扫描媒体文件 {args.input} ...")
    reporter.status(f"scanning {args.input} ...")

    # During a real run, persist files as they are scanned (throttled) so a
    # long scan survives interruption.
    # Dry-run stays read-only and passes no callback.
    on_file = None if args.dry_run else _file_persister(db)

    def _scan_progress(done: int, total: int, item, mode: str, retained: int) -> None:
        if coordinator and coordinator.is_cancelled():
            raise RuntimeError("scan cancelled by coordinator")
        reporter.status(f"scanning [{done}/{total}]: {item.rel_path}")
        web.update_web_scanner(
            is_scanning=True,
            count=done,
            total=total,
            current=item.rel_path,
            mode=mode,
            retained=retained,
        )

    result = scan(
        args.input,
        db,
        args.timezone,
        args.image_duration,
        only_camera_photos=args.only_camera_photos,
        only_phone_videos=args.only_phone_videos,
        force_scan=is_force_scan,
        segments_dir=getattr(args, "segments_dir", None),
        on_file=on_file,
        on_progress=_scan_progress,
    )
    if on_file is not None and hasattr(on_file, "flush"):
        on_file.flush()

    if coordinator and coordinator.is_cancelled():
        _log.info("pipeline execution cancelled after scanning")
        return _EXIT_OK

    web.update_web_scanner(
        is_scanning=False,
        count=len(result.files),
        total=len(result.files),
        current=None,
        mode="idle",
        retained=len(result.files),
    )
    if not is_force_scan and len(result.new_file_ids) == 0 and len(result.deleted_file_ids) == 0:
        reporter.status(f"media files match database index ({len(result.files)} files); skipped scanning")
        _log.info("media files match database index (%d files); full scan skipped", len(result.files))
        web.set_web_status("watching" if args.watch else "idle", f"就绪 ({len(result.files)} 个文件与数据库索引完全一致，已跳过扫描)")
    else:
        _log.info(
            "scan complete: %d media file(s) (%d new, %d already processed, %d deleted), "
            "%d error(s), %d skipped",
            len(result.files),
            len(result.new_file_ids),
            len(result.processed_file_ids),
            len(result.deleted_file_ids),
            len(result.errors),
            len(result.skipped),
        )
        for error in result.errors:
            reporter.status(f"  error: {error['path']}: {error['reason']}")
            _log.warning("scan error: %s: %s", error["path"], error["reason"])
        for item in result.skipped:
            reporter.status(f"  skipped: {item['path']} ({item['reason']})")
            _log.info("skipped: %s (%s)", item["path"], item["reason"])


    reporter.status(
        f"planning: {len(result.files)} files "
        f"({len(result.new_file_ids)} new, {len(result.deleted_file_ids)} deleted, "
        f"{len(result.skipped)} skipped)"
    )
    is_force_plan = getattr(args, "force_plan", False)
    existing_segs = {} if is_force_plan else db.segments
    plan_result = plan(
        result.files,
        existing_segs,
        args.segment_duration,
        params=_segment_params(args),
        flush=args.flush or is_force_plan,
        short_video_threshold=getattr(args, "short_video_threshold", 300.0),
        ignore_live_photos=getattr(args, "ignore_live_photos", True),
        min_video_duration=getattr(args, "min_video_duration", 2.0),
    )
    if coordinator and coordinator.is_cancelled():
        _log.info("pipeline execution cancelled after planning")
        return _EXIT_OK
    web.set_web_planned_segments(plan_result.segments)
    _log.info(
        "plan complete: %d segment(s) to process, %d skipped (unchanged), %d pending (held for flush)",
        len(plan_result.segments),
        len(plan_result.skipped_segment_ids),
        len(plan_result.pending_files),
    )

    files_by_id = {item.file_id: item for item in result.files}

    if args.dry_run:
        # Dry-run is a read-only preview: no database writes.
        reporter.status(
            f"dry-run: {len(plan_result.segments)} segment(s), "
            f"{len(plan_result.skipped_segment_ids)} skipped, "
            f"{len(plan_result.pending_files)} pending"
        )
        for segment in plan_result.segments:
            kind = "rebuild" if segment.is_rebuild else "new"
            reporter.status(
                f"  segment {segment.segment_id[:12]} {segment.title} "
                f"[{len(segment.file_ids)} files, {segment.duration_seconds:.1f}s] ({kind})"
            )
            _log.info(
                "dry-run segment %s: title=%s files=%d duration=%.1fs kind=%s",
                segment.segment_id,
                segment.title,
                len(segment.file_ids),
                segment.duration_seconds,
                kind,
            )
        for item in plan_result.pending_files:
            reporter.status(f"  pending file {item.file_id[:12]} {item.rel_path}")
            _log.info("pending file %s: %s", item.file_id, item.rel_path)
        _log.info("dry-run complete (no database writes, no transcode, no upload)")
        return 0

    # Real run: persist scanned files, then transcode and upload.
    from .scanner import normalize_rel_path
    if hasattr(db, "skipped_files"):
        for item in result.skipped:
            norm_p = normalize_rel_path(item.get("path", ""), args.input)
            if norm_p:
                db.skipped_files[norm_p] = item.get("reason", "skipped")
        for err in result.errors:
            norm_p = normalize_rel_path(err.get("path", ""), args.input)
            if norm_p:
                db.skipped_files[norm_p] = f"error: {err.get('reason', '')}"

    for item in result.files:
        db.upsert_file(item.file_id, item.to_record())
        _log.debug("indexed file %s: %s (%.1fs)", item.file_id[:12], item.rel_path, item.duration_seconds or 0.0)
    for file_id in result.deleted_file_ids:
        db.remove_file(file_id)
        _log.debug("removed deleted file %s from index", file_id[:12])

    # Immediately save the database after scanning so index on disk is up to date
    db.save()
    _log.info("database saved after scan sync: %d file(s)", len(db.files))

    # Immediately persist all planned segments into db.segments so they are visible on Web UI
    segments_dir = getattr(args, "segments_dir", None) or os.path.join(os.path.dirname(args.db), "uploaded_segments")
    os.makedirs(segments_dir, exist_ok=True)

    if is_force_plan:
        new_sids = {seg.segment_id for seg in plan_result.segments}
        for sid in list(db.segments.keys()):
            if sid not in new_sids:
                db.segments.pop(sid, None)

    for seg in plan_result.segments:
        existing_srec = db.segments.get(seg.segment_id, {})
        local_file = None
        if coordinator:
            local_file = coordinator.find_local_segment_file(seg.segment_id, seg.title)
        if not local_file:
            for cand in [os.path.join(segments_dir, f"{seg.title}.mp4"), os.path.join(segments_dir, f"{seg.segment_id}.mp4")]:
                if os.path.isfile(cand) and os.path.getsize(cand) > 1024:
                    local_file = cand
                    break

        has_local = bool(local_file and os.path.isfile(local_file))
        existing_vid = existing_srec.get("youtube_video_id")
        initial_status = existing_srec.get("status")
        if not initial_status:
            initial_status = SEGMENT_STATUS_SEALED if (existing_vid or has_local) else "pending"

        db.upsert_segment(
            seg.segment_id,
            {
                "title": seg.title,
                "file_ids": list(seg.file_ids),
                "range": [seg.start_ts, seg.end_ts],
                "duration_seconds": seg.duration_seconds,
                "youtube_video_id": existing_vid,
                "previous_video_ids": list(existing_srec.get("previous_video_ids", [])),
                "status": initial_status,
                "chapters": list(existing_srec.get("chapters", [])),
                "last_rebuilt_at": existing_srec.get("last_rebuilt_at"),
                "attempts": existing_srec.get("attempts", 0),
                "error": existing_srec.get("error"),
            }
        )
    db.save()
    _log.info("database saved after plan sync: %d segment(s)", len(db.segments))

    # Real run: transcode each new/rebuild/pending segment, then upload.

    # Identify any pending, unsealed, or unuploaded segments in the database
    # (e.g. planned previously, pending retry, or transcoded under --no-upload).
    planned_sids = {seg.segment_id for seg in plan_result.segments}
    db_unprocessed_segs = []
    is_no_upload = getattr(args, "no_upload", False) or (uploader is None)

    for sid, srec in db.segments.items():
        if sid in planned_sids:
            continue
        # If already uploaded to YouTube, skip
        if srec.get("youtube_video_id"):
            continue
        # If --no-upload and already sealed locally, check if local file exists
        if is_no_upload and srec.get("status") == SEGMENT_STATUS_SEALED:
            local_f = None
            if coordinator:
                local_f = coordinator.find_local_segment_file(sid, srec.get("title", ""))
            if not local_f:
                for cand in [os.path.join(segments_dir, f"{srec.get('title', '')}.mp4"), os.path.join(segments_dir, f"{sid}.mp4")]:
                    if os.path.isfile(cand) and os.path.getsize(cand) > 0:
                        local_f = cand
                        break
            if local_f and os.path.isfile(local_f):
                continue

        file_ids = srec.get("file_ids", [])
        rng = srec.get("range") or ["", ""]
        db_unprocessed_segs.append(
            Segment(
                file_ids=file_ids,
                start_ts=rng[0] if len(rng) > 0 else "",
                end_ts=rng[1] if len(rng) > 1 else "",
                duration_seconds=float(srec.get("duration_seconds", 0.0)),
                segment_id=sid,
                title=srec.get("title") or sid[:16],
            )
        )

    if db_unprocessed_segs:
        db_unprocessed_segs.sort(key=lambda s: (s.start_ts or "", s.title))
        pending_cnt = sum(1 for s in db_unprocessed_segs if db.segments.get(s.segment_id, {}).get("status") != SEGMENT_STATUS_SEALED)
        sealed_cnt = len(db_unprocessed_segs) - pending_cnt
        if pending_cnt > 0:
            _log.info("found %d pending segment(s) in database; queuing for transcode", pending_cnt)
            reporter.status(f"found {pending_cnt} pending segment(s) in database; queuing for transcode")
        if sealed_cnt > 0:
            _log.info("found %d sealed unuploaded segment(s) in database; queuing for upload", sealed_cnt)
            reporter.status(f"found {sealed_cnt} unuploaded segment(s) in database; queuing for upload")

    all_to_process = list(plan_result.segments) + db_unprocessed_segs
    # Deduplicate preserving order
    seen_sids = set()
    segments_to_process = []
    for s in all_to_process:
        if s.segment_id not in seen_sids:
            seen_sids.add(s.segment_id)
            segments_to_process.append(s)

    segments_to_process.sort(key=lambda s: (s.start_ts or "", s.title))
    web.set_web_planned_segments(segments_to_process)

    # Reconcile against YouTube so a lost local database does not re-upload
    # everything: only list when there is actually something to upload.
    remote_index: dict[str, str] = {}
    if segments_to_process and uploader is not None:
        reporter.status("listing existing uploads on YouTube ...")
        remote_index = fetch_remote_index(uploader.service)
        if remote_index:
            reporter.status(f"found {len(remote_index)} segment(s) already on YouTube")
        all_vids = getattr(remote_index, "all_video_ids", None)
        success = getattr(remote_index, "success", True)
        reconcile_database(db, remote_index, all_vids if success else None)

    config = TranscodeConfig(
        crf=args.crf,
        max_resolution=args.max_resolution,
        canvas_mode=args.canvas_mode,
        ken_burns=args.ken_burns,
        image_duration=args.image_duration,
        fps=args.fps,
        preset=args.x264_preset,
        encoder=getattr(args, "encoder", "auto"),
    )

    active_encoder = resolve_encoder(config.encoder)
    reporter.status(f"video encoder: {active_encoder}")
    _log.info("video encoder active: %s (config: %s)", active_encoder, config.encoder)

    segments_dir = getattr(args, "segments_dir", None) or os.path.join(os.path.dirname(args.db), "uploaded_segments")
    os.makedirs(segments_dir, exist_ok=True)

    is_no_upload = getattr(args, "no_upload", False) or (uploader is None)
    upload_worker = None
    if not is_no_upload and uploader:
        from .uploader import UploadTask, UploadWorker
        upload_worker = UploadWorker(uploader, reporter=reporter)
        if coordinator:
            coordinator._active_upload_worker = upload_worker

    total_segments = len(segments_to_process)
    try:
        for index, segment in enumerate(segments_to_process, start=1):
            if coordinator and coordinator.is_cancelled():
                _log.info("pipeline execution cancelled; exiting segment loop at %d/%d", index, total_segments)
                break
            try:
                from . import web
                if web.is_transcode_paused():
                    _log.info("transcode is paused; waiting before segment %s...", segment.title)
                    web.wait_if_transcode_paused()
            except Exception:
                pass
            if coordinator and coordinator.is_cancelled():
                _log.info("pipeline execution cancelled before processing segment %s", segment.title)
                break
            srec = db.segments.get(segment.segment_id)
            excluded_ids = set(srec.get("excluded_file_ids", [])) if srec else set()
            segment_files = []
            for fid in segment.file_ids:
                if fid in excluded_ids:
                    continue
                f_item = files_by_id.get(fid)
                if not f_item and fid in db.files:
                    f_item = ScannedFile.from_record(fid, db.files[fid], args.input)
                    files_by_id[fid] = f_item
                if f_item:
                    segment_files.append(f_item)
            if not segment_files:
                _log.info("segment %s has no active files (all excluded); skipping", segment.segment_id)
                continue
            is_single_vid = len(segment_files) == 1 and segment_files[0].type == FILE_TYPE_VIDEO
            if is_single_vid:
                ext = os.path.splitext(segment_files[0].abs_path)[1].lower() or ".mp4"
                out_path = os.path.join(segments_dir, f"{segment.title}{ext}")
            else:
                out_path = os.path.join(segments_dir, f"{segment.title}.mp4")

            # Already on YouTube (e.g. after a lost db or restart): record locally and skip.
            existing_video = remote_index.get(segment.segment_id) or remote_index.get(segment.segment_id[:16])
            if existing_video:
                reporter.status(
                    f"segment {segment.title} already on YouTube ({existing_video}); skipping"
                )
                _log.info(
                    "segment %s already uploaded as %s; recording and skipping",
                    segment.segment_id,
                    existing_video,
                )
                db.upsert_segment(
                    segment.segment_id,
                    {
                        "title": segment.title,
                        "file_ids": segment.file_ids,
                        "range": [segment.start_ts, segment.end_ts],
                        "duration_seconds": segment.duration_seconds,
                        "output_path": None,
                        "youtube_video_id": existing_video,
                        "previous_video_ids": [],
                        "status": SEGMENT_STATUS_SEALED,
                        "chapters": [],
                        "last_rebuilt_at": None,
                        "attempts": 0,
                        "error": None,
                    },
                )
                if segment.is_rebuild and segment.replaces_segment_id:
                    db.segments.pop(segment.replaces_segment_id, None)
                db.save()
                continue

            # Check if segment was already transcoded locally and sealed in db
            srec = db.segments.get(segment.segment_id)
            local_out = None
            if coordinator:
                local_out = coordinator.find_local_segment_file(segment.segment_id, segment.title)
            if not local_out:
                for cand in [out_path, os.path.join(segments_dir, f"{segment.title}.mp4"), os.path.join(segments_dir, f"{segment.segment_id}.mp4")]:
                    if os.path.isfile(cand) and os.path.getsize(cand) > 0:
                        local_out = cand
                        break
            has_existing_file = bool(local_out and os.path.isfile(local_out))
            if (
                srec
                and not segment.is_rebuild
                and has_existing_file
                and not srec.get("youtube_video_id")
            ):
                target_out = local_out
                if upload_worker:
                    reporter.status(f"segment {segment.title} already transcoded locally ({target_out}); queuing for upload")
                    _log.info("segment %s already transcoded locally; queuing for upload", segment.segment_id)
                    description = build_segment_description(segment_files, total_duration=segment.duration_seconds)

                    def _make_callbacks(sid, tit, seg_out):
                        def _done(vid: str):
                            s = db.segments.get(sid, {})
                            s["youtube_video_id"] = vid
                            s["status"] = SEGMENT_STATUS_SEALED
                            s["error"] = None
                            db.upsert_segment(sid, s)
                            db.save()
                            rotate_uploaded_segments(segments_dir, getattr(args, "keep_segments", 0))
                            web.finish_web_segment(sid, youtube_video_id=vid)

                        def _err(e: Exception):
                            s = db.segments.get(sid, {})
                            s["error"] = str(e)
                            db.upsert_segment(sid, s)
                            db.save()

                        return _done, _err

                    cb_done, cb_err = _make_callbacks(segment.segment_id, segment.title, target_out)
                    upload_worker.enqueue(
                        UploadTask(
                            segment_id=segment.segment_id,
                            title=segment.title,
                            out_path=target_out,
                            description=description,
                            privacy=args.privacy,
                            on_complete=cb_done,
                            on_error=cb_err,
                        )
                    )
                    continue
                else:
                    reporter.status(f"segment {segment.title} already transcoded locally ({target_out})")
                    _log.info("segment %s already transcoded locally", segment.segment_id)
                    srec["status"] = SEGMENT_STATUS_SEALED
                    srec["error"] = None
                    db.upsert_segment(segment.segment_id, srec)
                    db.save()
                    web.finish_web_segment(segment.segment_id, youtube_video_id=None)
                    continue

            _log.info(
                "segment %d/%d: %s (%d files, %.1fs) -> %s",
                index,
                total_segments,
                segment.title,
                len(segment_files),
                segment.duration_seconds,
                out_path,
            )

            def transcode_fn(files, _out_path=out_path):
                def progress(
                    done: int,
                    total: int,
                    item,
                    is_resumed: bool = False,
                    file_progress: float | None = None,
                    fps: float | None = None,
                    speed: str | None = None,
                ) -> None:
                    if is_resumed:
                        reporter.status(f"    [断点复用 {done}/{total}] {item.rel_path}")
                    elif file_progress is not None:
                        spd_info = f" [速度 {speed}]" if speed and speed != "N/A" else ""
                        fps_info = f" ({fps:.1f} fps)" if fps and fps > 0 else ""
                        reporter.status(
                            f"    transcode {done}/{total}: {item.rel_path} ({file_progress:.1f}%{spd_info}{fps_info})"
                        )
                    else:
                        reporter.status(f"    transcode {done}/{total}: {item.rel_path} ({item.type})")
                    web.update_web_transcode(
                        segment_id=segment.segment_id,
                        title=segment.title,
                        done=done,
                        total=total,
                        current_file=item.rel_path,
                        resumed=is_resumed,
                        file_progress=file_progress,
                        fps=fps,
                        speed=speed,
                        encoder=active_encoder,
                    )

                return transcode_segment(files, _out_path, config, progress=progress)

            try:
                reporter.status(f"transcoding {segment.title} ...")
                web.set_web_status("transcoding", f"正在转码 {segment.title}")
                if segment.is_rebuild:
                    reporter.status(
                        f"rebuilding {segment.title} (replaces {segment.replaces_segment_id[:12]} ...) ..."
                    )
                    _log.info(
                        "rebuild: replacing segment %s with %s",
                        segment.replaces_segment_id,
                        segment.segment_id,
                    )
                    if upload_worker:
                        upload_worker.wait_done()
                    if not is_no_upload and uploader:
                        rebuilder = Rebuilder(
                            db,
                            transcode_fn=transcode_fn,
                            upload_fn=lambda out, title, desc: uploader.upload(
                                out, title, desc, privacy=args.privacy
                            ),
                            verify_fn=uploader.verify,
                            delete_fn=uploader.delete_video,
                        )
                        video_id = rebuilder.rebuild(
                            segment.replaces_segment_id,
                            segment,
                            segment_files,
                            title=segment.title,
                        )
                        _log.info("rebuild committed: new video id %s", video_id)
                    else:
                        _, chapters = transcode_fn(segment_files)
                        video_id = None
                        db.upsert_segment(
                            segment.segment_id,
                            {
                                "title": segment.title,
                                "file_ids": segment.file_ids,
                                "range": [segment.start_ts, segment.end_ts],
                                "duration_seconds": segment.duration_seconds,
                                "youtube_video_id": None,
                                "previous_video_ids": [],
                                "status": SEGMENT_STATUS_SEALED,
                                "chapters": chapters,
                                "last_rebuilt_at": None,
                                "attempts": 0,
                                "error": None,
                            },
                        )
                        if segment.replaces_segment_id:
                            db.segments.pop(segment.replaces_segment_id, None)

                    if not is_no_upload:
                        rotate_uploaded_segments(segments_dir, getattr(args, "keep_segments", 0))
                    web.finish_web_segment(segment.segment_id, youtube_video_id=video_id)
                else:
                    _, chapters = transcode_fn(segment_files)
                    if upload_worker:
                        reporter.status(f"transcoded {segment.title}; queuing for background upload ...")
                        _log.info("transcoded %s; queuing for background upload", segment.segment_id)
                        srec = db.segments.get(segment.segment_id, {})
                        db.upsert_segment(
                            segment.segment_id,
                            {
                                "title": segment.title,
                                "file_ids": segment.file_ids,
                                "range": [segment.start_ts, segment.end_ts],
                                "duration_seconds": segment.duration_seconds,
                                "youtube_video_id": None,
                                "previous_video_ids": srec.get("previous_video_ids", []),
                                "status": SEGMENT_STATUS_SEALED,
                                "chapters": chapters if chapters else srec.get("chapters", []),
                                "last_rebuilt_at": srec.get("last_rebuilt_at"),
                                "attempts": srec.get("attempts", 0),
                                "error": None,
                            },
                        )
                        db.save()

                        description = build_segment_description(segment_files, total_duration=segment.duration_seconds)
                        def _make_callbacks(sid, tit, seg_out):
                            def _done(vid: str):
                                s = db.segments.get(sid, {})
                                s["youtube_video_id"] = vid
                                s["status"] = SEGMENT_STATUS_SEALED
                                s["error"] = None
                                db.upsert_segment(sid, s)
                                db.save()
                                rotate_uploaded_segments(segments_dir, getattr(args, "keep_segments", 0))
                                web.finish_web_segment(sid, youtube_video_id=vid)

                            def _err(e: Exception):
                                s = db.segments.get(sid, {})
                                s["error"] = str(e)
                                db.upsert_segment(sid, s)
                                db.save()

                            return _done, _err

                        cb_done, cb_err = _make_callbacks(segment.segment_id, segment.title, out_path)
                        upload_worker.enqueue(
                            UploadTask(
                                segment_id=segment.segment_id,
                                title=segment.title,
                                out_path=out_path,
                                description=description,
                                privacy=args.privacy,
                                on_complete=cb_done,
                                on_error=cb_err,
                            )
                        )
                    elif not is_no_upload and uploader:
                        reporter.status(f"uploading {segment.title} ...")
                        web.set_web_status("uploading", f"正在上传 {segment.title}")
                        description = build_segment_description(segment_files, total_duration=segment.duration_seconds)
                        def _upload_cb(pct: float) -> None:
                            web.set_web_status("uploading", f"正在上传 {segment.title} ({pct * 100:.1f}%)")

                        video_id = uploader.upload(
                            out_path,
                            segment.title,
                            description,
                            privacy=args.privacy,
                            progress_callback=_upload_cb,
                        )
                        _log.info("sealed segment %s -> video id %s", segment.segment_id, video_id)
                        saved_out_path = out_path if (getattr(args, "keep_segments", 0) > 0 and os.path.exists(out_path)) else None
                        srec = db.segments.get(segment.segment_id, {})
                        db.upsert_segment(
                            segment.segment_id,
                            {
                                "title": segment.title,
                                "file_ids": segment.file_ids,
                                "range": [segment.start_ts, segment.end_ts],
                                "duration_seconds": segment.duration_seconds,
                                "output_path": saved_out_path,
                                "youtube_video_id": video_id,
                                "previous_video_ids": srec.get("previous_video_ids", []),
                                "status": SEGMENT_STATUS_SEALED,
                                "chapters": chapters if chapters else srec.get("chapters", []),
                                "last_rebuilt_at": srec.get("last_rebuilt_at"),
                                "attempts": srec.get("attempts", 0),
                                "error": None,
                            },
                        )
                        rotate_uploaded_segments(segments_dir, getattr(args, "keep_segments", 0))
                        web.finish_web_segment(segment.segment_id, youtube_video_id=video_id)
                    else:
                        video_id = None
                        reporter.status(f"transcoded {segment.title} (saved locally to {out_path})")
                        _log.info("sealed segment %s locally (no-upload)", segment.segment_id)
                        saved_out_path = out_path
                        srec = db.segments.get(segment.segment_id, {})
                        db.upsert_segment(
                            segment.segment_id,
                            {
                                "title": segment.title,
                                "file_ids": segment.file_ids,
                                "range": [segment.start_ts, segment.end_ts],
                                "duration_seconds": segment.duration_seconds,
                                "output_path": saved_out_path,
                                "youtube_video_id": None,
                                "previous_video_ids": srec.get("previous_video_ids", []),
                                "status": SEGMENT_STATUS_SEALED,
                                "chapters": chapters if chapters else srec.get("chapters", []),
                                "last_rebuilt_at": srec.get("last_rebuilt_at"),
                                "attempts": srec.get("attempts", 0),
                                "error": None,
                            },
                        )
                        web.finish_web_segment(segment.segment_id, youtube_video_id=None)
            except QuotaExceededError as exc:
                reporter.status(
                    f"YouTube quota exhausted; deferring remaining "
                    f"{total_segments - index + 1} segment(s) to a later retry"
                )
                _log.warning(
                    "quota exhausted while processing segment %s: %s", segment.segment_id, exc
                )
                try:
                    os.remove(out_path)
                except OSError:
                    pass
                db.save()
                return _EXIT_QUOTA

            db.save()
            _log.debug("database saved to %s", db.path)

        if upload_worker:
            if coordinator and coordinator.is_cancelled():
                _log.info("pipeline cancelled; skipping wait for background upload worker")
                upload_worker.stop()
            else:
                reporter.status("transcoding complete; waiting for background upload(s) to finish ...")
                _log.info("transcoding complete; waiting for background upload worker to drain")
                upload_worker.wait_done()
                if upload_worker.quota_error is not None:
                    reporter.status("YouTube quota exhausted; deferring remaining upload(s) to next run")
                    _log.warning("quota exhausted during background upload: %s", upload_worker.quota_error)
                    db.save()
                    return _EXIT_QUOTA
    finally:
        if coordinator and coordinator._active_upload_worker is upload_worker:
            coordinator._active_upload_worker = None
        if upload_worker:
            upload_worker.stop()

    db.save()
    web.set_web_status("watching" if args.watch else "done", "常驻监听中" if args.watch else "已完成")
    _log.info(
        "pipeline done: %d segment(s) processed, %d pending",
        len(segments_to_process),
        len(plan_result.pending_files),
    )
    reporter.status("done")
    return _EXIT_OK


def run_watch(args: argparse.Namespace, reporter: Reporter | None = None) -> int:
    """Process once, then keep re-processing as new media files appear.

    New files are detected by watchdog (inotify/FSEvents/...), with a periodic
    directory-mtime scan as a safety net for file systems where events are not
    delivered (e.g. network shares). Quota exhaustion is handled by backing off
    and retrying, without keeping any local quota state.
    """
    import signal
    import threading

    from .watcher import MtimeScanner, Watcher

    # docker stop 发 SIGTERM，转成 KeyboardInterrupt 走同样的 flush 退出逻辑
    signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(KeyboardInterrupt()))

    reporter = reporter or Reporter()

    try:
        code = run_pipeline(args, reporter)
        if code == _EXIT_ERROR:
            return code
    except KeyboardInterrupt:
        reporter.status("stopping on signal ...")
        _log.info("received exit/interrupt signal during initial pipeline; shutting down cleanly")
        return 0
    args.force_scan = False  # subsequent watch runs use incremental scan

    reporter.status(
        f"watching for new files (watchdog + mtime every {args.mtime_interval:g}s, "
        f"quiet {args.quiet_period:g}s, Ctrl+C to stop) ..."
    )
    _log.info(
        "watch mode active: watchdog + mtime every %.1fs, quiet %.1fs, quota backoff %.1fs",
        args.mtime_interval,
        args.quiet_period,
        args.quota_backoff,
    )

    changed = threading.Event()

    def on_new_file(path: str) -> None:
        _log.info("watchdog detected new file: %s", path)
        changed.set()

    watcher = Watcher(args.input, on_new_file, use_watchdog=True)
    try:
        watcher.start()
    except Exception as exc:  # noqa: BLE001 - fall back to mtime polling
        _log.warning("watchdog unavailable (%s); using mtime polling only", exc)

    mtime_scanner = MtimeScanner(args.input)
    mtime_scanner.scan()  # baseline
    last_mtime_scan = time.monotonic()
    last_change = time.monotonic()
    has_changes = False
    quota_retry_at = time.monotonic() + args.quota_backoff if code == _EXIT_QUOTA else 0.0

    try:
        while True:
            # Wake on a watchdog event, or after the internal tick.
            if changed.wait(timeout=max(1.0, args.poll_interval)):
                changed.clear()
                has_changes = True
                last_change = time.monotonic()
            now = time.monotonic()

            # Safety net: cheap directory-mtime scan.
            if now - last_mtime_scan >= args.mtime_interval:
                last_mtime_scan = now
                changed_dirs = mtime_scanner.scan()
                if changed_dirs:
                    reporter.status(f"mtime scan: {len(changed_dirs)} directory(ies) changed")
                    _log.info(
                        "mtime scan detected %d changed directory(ies)", len(changed_dirs)
                    )
                    has_changes = True
                    last_change = now

            # Quota backoff: retry once the backoff has elapsed.
            if quota_retry_at and now >= quota_retry_at:
                coord = _get_matched_coordinator(args)
                if coord and (coord.is_pipeline_active() or coord.is_task_running()):
                    time.sleep(1.0)
                    continue
                quota_retry_at = 0.0
                reporter.status("quota backoff elapsed; retrying ...")
                _log.info("quota backoff elapsed; re-running pipeline")
                status = run_pipeline(args, reporter, force_scan=False)
                mtime_scanner.scan()
                last_mtime_scan = time.monotonic()
                has_changes = False
                if status == _EXIT_QUOTA:
                    quota_retry_at = time.monotonic() + args.quota_backoff
                continue

            # Process new files after the quiet period (debounce).
            if has_changes and now - last_change >= args.quiet_period:
                coord = _get_matched_coordinator(args)
                if coord and (coord.is_pipeline_active() or coord.is_task_running()):
                    time.sleep(1.0)
                    continue
                reporter.status("quiet period elapsed; re-processing ...")
                _log.info("quiet period (%.1fs) elapsed; re-running pipeline", args.quiet_period)
                status = run_pipeline(args, reporter, force_scan=False)
                mtime_scanner.scan()
                last_mtime_scan = time.monotonic()
                has_changes = False
                if status == _EXIT_QUOTA:
                    quota_retry_at = time.monotonic() + args.quota_backoff
    except KeyboardInterrupt:
        reporter.status("stopping watch mode on signal ...")
        _log.info("received exit/interrupt signal; shutting down cleanly")
    finally:
        watcher.stop()
    return 0


def _build_uploader(args: argparse.Namespace, db: Database):
    from .uploader import Uploader

    db_dir = os.path.dirname(args.db)
    token_path = os.path.join(db_dir, "token.json")
    client_secret = args.client_secret or os.path.join(db_dir, "client_secret.json")
    web_port = getattr(args, "web_port", 8080)

    try:
        credentials = auth.ensure_credentials(
            client_secret,
            token_path=token_path,
            web_port=web_port,
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("cannot obtain YouTube credentials: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return None

    from googleapiclient.discovery import build

    service = build("youtube", "v3", credentials=credentials)
    return Uploader(service, playlist_id=getattr(args, "playlist", None))


def _run_login(args: argparse.Namespace) -> int:
    """Run the OAuth login flow and save token.json next to the database."""
    db_dir = os.path.dirname(args.db)
    token_path = os.path.join(db_dir, "token.json")
    client_secret = args.client_secret or os.path.join(db_dir, "client_secret.json")
    _log.info("OAuth login: client secret %s -> token %s", client_secret, token_path)
    if not os.path.exists(client_secret):
        _log.error("client secret not found: %s", client_secret)
        print(f"client secret not found: {client_secret}", file=sys.stderr)
        return _EXIT_ERROR
    try:
        auth.headless_oauth_flow(client_secret, token_path=token_path)
    except Exception as exc:  # noqa: BLE001
        _log.error("OAuth login failed: %s", exc)
        print(f"OAuth login failed: {exc}", file=sys.stderr)
        return _EXIT_ERROR
    _log.info("token saved to %s", token_path)
    print(f"token saved to {token_path}")
    return _EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Detailed audit log goes next to the database by default; the console
    # (stderr) shows WARNING+ unless -v / -vv is given.
    log_file = args.log_file if args.log_file is not None else (args.db + ".log")
    setup_logging(verbose=args.verbose, log_file=log_file)
    _log.info("tubetape starting (log file: %s)", log_file)

    if args.login:
        return _run_login(args)

    coordinator = AppCoordinator(args)
    setattr(args, "_coordinator", coordinator)
    coordinator.load_saved_config()

    if getattr(args, "web_port", 0) > 0 and not args.dry_run:
        from .web import WebServer, set_app_coordinator, set_web_context

        set_app_coordinator(coordinator)
        set_web_context(
            media_dir=args.input,
            db=coordinator.db,
            db_path=args.db,
            keep_segments=getattr(args, "keep_segments", 0),
        )
        web_server = WebServer(
            port=args.web_port,
            log_file=log_file,
            media_dir=args.input,
            db_path=args.db,
            keep_segments=getattr(args, "keep_segments", 0),
        )
        web_server.start()

    import signal

    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(KeyboardInterrupt()))
        except (ValueError, AttributeError):
            pass

    try:
        if args.watch and not args.dry_run:
            return run_watch(args)
        return run_pipeline(args)
    except KeyboardInterrupt:
        _log.info("received exit/interrupt signal; shutting down cleanly")
        return 0


if __name__ == "__main__":
    sys.exit(main())
