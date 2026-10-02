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
from .chapters import chapters_text
from .db import SEGMENT_STATUS_SEALED, Database
from .log import get_logger, setup_logging
from .planner import plan
from .rebuild import Rebuilder
from .reconcile import embed_segment_id, fetch_remote_index
from .scanner import scan
from .transcoder import TranscodeConfig, transcode_segment
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
        "--segment-duration",
        type=durations.parse_duration,
        default="1h",
        help="max duration per uploaded video; supports 1h / 3600s / 1:00:00 "
        "(default: 1h)",
    )
    parser.add_argument(
        "--timezone",
        type=durations.parse_timezone,
        default=None,
        help="timezone used to interpret timezone-less EXIF times "
        "(default: system local)",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=16,
        help="video re-encode quality; lower is better (default: 16)",
    )
    parser.add_argument(
        "--max-resolution",
        type=durations.parse_resolution,
        default="7680x4320",
        help="resolution ceiling WxH, never upscaling (default: 7680x4320). "
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
        default=60,
        help="output frame rate (default: 60; preserves up to 60fps sources)",
    )
    parser.add_argument(
        "--x264-preset",
        choices=[
            "ultrafast", "superfast", "veryfast", "faster", "fast",
            "medium", "slow", "slower", "veryslow",
        ],
        default="slow",
        help="x264 speed/efficiency preset; slower = better quality per bitrate "
        "(default: slow)",
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
        default="1h",
        help="in watch mode, interval for the directory-mtime safety-net scan "
        "(catches events watchdog/inotify misses; default: 1h)",
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
        args.timezone = durations.system_local_timezone()

    if args.crf < 0:
        parser.error("--crf must be >= 0")
    if args.image_duration <= 0:
        parser.error("--image-duration must be > 0")
    if args.segment_duration <= 0:
        parser.error("--segment-duration must be > 0")
    if args.fps <= 0:
        parser.error("--fps must be > 0")

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
    )


def _file_persister(db: Database):
    """Return a scan callback that upserts files and periodically saves the db.

    Saving every N files (or every few seconds) means a long scan can be
    interrupted without losing all of its progress: already-scanned files are
    already persisted with their size+mtime, so the next run reuses the cache.
    """
    state = {"count": 0, "last_save": time.monotonic()}

    def persist(item) -> None:
        db.upsert_file(item.file_id, item.to_record())
        state["count"] += 1
        now = time.monotonic()
        if state["count"] >= 1000 or now - state["last_save"] >= 30.0:
            db.save()
            _log.debug("incremental save: %d file(s) indexed so far", len(db.files))
            state["count"] = 0
            state["last_save"] = now

    return persist


def run_pipeline(args: argparse.Namespace, reporter: Reporter | None = None) -> int:
    reporter = reporter or Reporter()
    db = Database.load(args.db)

    _log.info(
        "pipeline start: input=%s db=%s timezone=%s segment_duration=%.1fs "
        "crf=%d max_resolution=%s ken_burns=%s privacy=%s watch=%s dry_run=%s "
        "only_camera_photos=%s only_phone_videos=%s flush=%s",
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
    )
    _log.info("database loaded: %d file(s), %d segment(s)", len(db.files), len(db.segments))

    reporter.status(f"scanning {args.input} ...")
    # During a real run, persist files as they are scanned (throttled) so a
    # long scan survives interruption and the hash cache is useful next time.
    # Dry-run stays read-only and passes no callback.
    on_file = None if args.dry_run else _file_persister(db)
    result = scan(
        args.input,
        db,
        args.timezone,
        args.image_duration,
        only_camera_photos=args.only_camera_photos,
        only_phone_videos=args.only_phone_videos,
        on_file=on_file,
    )
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
    plan_result = plan(
        result.files,
        db.segments,
        args.segment_duration,
        params=_segment_params(args),
        flush=args.flush,
    )
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
    for item in result.files:
        db.upsert_file(item.file_id, item.to_record())
        _log.debug("indexed file %s: %s (%.1fs)", item.file_id[:12], item.rel_path, item.duration_seconds or 0.0)
    for file_id in result.deleted_file_ids:
        db.remove_file(file_id)
        _log.debug("removed deleted file %s from index", file_id[:12])

    # Real run: transcode each new/rebuild segment, then upload.
    uploader = _build_uploader(args, db)
    if uploader is None:
        reporter.status("no YouTube credentials; set TUBETAPE_TOKEN or token.json")
        _log.error("no YouTube credentials found (TUBETAPE_TOKEN env or token.json next to db)")
        db.save()
        _log.info("database saved to %s", db.path)
        return _EXIT_ERROR

    # Reconcile against YouTube so a lost local database does not re-upload
    # everything: only list when there is actually something to upload.
    remote_index: dict[str, str] = {}
    if plan_result.segments:
        reporter.status("listing existing uploads on YouTube ...")
        remote_index = fetch_remote_index(uploader.service)
        if remote_index:
            reporter.status(f"found {len(remote_index)} segment(s) already on YouTube")

    config = TranscodeConfig(
        crf=args.crf,
        max_resolution=args.max_resolution,
        canvas_mode=args.canvas_mode,
        ken_burns=args.ken_burns,
        image_duration=args.image_duration,
        fps=args.fps,
        preset=args.x264_preset,
    )

    total_segments = len(plan_result.segments)
    for index, segment in enumerate(plan_result.segments, start=1):
        segment_files = [files_by_id[fid] for fid in segment.file_ids if fid in files_by_id]
        out_path = os.path.join(os.path.dirname(args.db), f".{segment.segment_id[:12]}.mp4")

        # Already on YouTube (e.g. after a lost db): record locally and skip.
        existing_video = None if segment.is_rebuild else remote_index.get(segment.segment_id)
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
            db.save()
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
            def progress(done: int, total: int, item) -> None:
                reporter.status(f"    transcode {done}/{total}: {item.rel_path} ({item.type})")

            return transcode_segment(files, _out_path, config, progress=progress)

        try:
            reporter.status(f"transcoding {segment.title} ...")
            if segment.is_rebuild:
                reporter.status(
                    f"rebuilding {segment.title} (replaces {segment.replaces_segment_id[:12]} ...) ..."
                )
                _log.info(
                    "rebuild: replacing segment %s with %s",
                    segment.replaces_segment_id,
                    segment.segment_id,
                )
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
                reporter.status(f"uploading {segment.title} ...")
                description = embed_segment_id(
                    chapters_text(chapters), segment.segment_id
                )
                video_id = uploader.upload(
                    out_path,
                    segment.title,
                    description,
                    privacy=args.privacy,
                )
                db.upsert_segment(
                    segment.segment_id,
                    {
                        "file_ids": segment.file_ids,
                        "range": [segment.start_ts, segment.end_ts],
                        "duration_seconds": segment.duration_seconds,
                        "output_path": out_path,
                        "youtube_video_id": video_id,
                        "previous_video_ids": [],
                        "status": SEGMENT_STATUS_SEALED,
                        "chapters": chapters,
                        "last_rebuilt_at": None,
                        "attempts": 0,
                        "error": None,
                    },
                )
                _log.info("sealed segment %s -> video id %s", segment.segment_id, video_id)
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

        # Clean up the local transcode output now that it's uploaded.
        try:
            os.remove(out_path)
            _log.debug("removed local transcode output %s", out_path)
        except OSError as exc:
            _log.debug("could not remove %s: %s", out_path, exc)

        db.save()
        _log.debug("database saved to %s", db.path)

    db.save()
    _log.info(
        "pipeline done: %d segment(s) processed, %d pending",
        len(plan_result.segments),
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

    code = run_pipeline(args, reporter)
    if code == _EXIT_ERROR:
        return code

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
                quota_retry_at = 0.0
                reporter.status("quota backoff elapsed; retrying ...")
                _log.info("quota backoff elapsed; re-running pipeline")
                status = run_pipeline(args, reporter)
                mtime_scanner.scan()
                last_mtime_scan = time.monotonic()
                has_changes = False
                if status == _EXIT_QUOTA:
                    quota_retry_at = time.monotonic() + args.quota_backoff
                continue

            # Process new files after the quiet period (debounce).
            if has_changes and now - last_change >= args.quiet_period:
                reporter.status("quiet period elapsed; re-processing ...")
                _log.info("quiet period (%.1fs) elapsed; re-running pipeline", args.quiet_period)
                status = run_pipeline(args, reporter)
                mtime_scanner.scan()
                last_mtime_scan = time.monotonic()
                has_changes = False
                if status == _EXIT_QUOTA:
                    quota_retry_at = time.monotonic() + args.quota_backoff
    except KeyboardInterrupt:
        reporter.status("exit signal: flushing pending segments ...")
        _log.info("received exit signal; flushing pending segments")
        args.flush = True
        run_pipeline(args, reporter)
    finally:
        watcher.stop()
    return 0


def _build_uploader(args: argparse.Namespace, db: Database):
    import os

    from .uploader import Uploader

    token_str = os.environ.get(auth.TOKEN_ENV)
    if not token_str:
        token_path = os.path.join(os.path.dirname(args.db), "token.json")
        if not os.path.exists(token_path):
            return None
        credentials = auth.load_token_file(token_path)
    else:
        credentials = auth.credentials_from_token_string(token_str)

    from googleapiclient.discovery import build

    service = build("youtube", "v3", credentials=credentials)
    return Uploader(service, playlist_id=getattr(args, "playlist", None))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Detailed audit log goes next to the database by default; the console
    # (stderr) shows WARNING+ unless -v / -vv is given.
    log_file = args.log_file if args.log_file is not None else (args.db + ".log")
    setup_logging(verbose=args.verbose, log_file=log_file)
    _log.info("tubetape starting (log file: %s)", log_file)

    if args.watch and not args.dry_run:
        return run_watch(args)
    return run_pipeline(args)


if __name__ == "__main__":
    sys.exit(main())
