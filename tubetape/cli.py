"""Command-line interface for TubeTape.

Parses every startup parameter from prompt.md and runs the pipeline:
scan -> plan -> (dry-run: stop) -> transcode -> upload -> watch.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import auth, durations
from .chapters import chapters_text
from .db import Database
from .planner import plan
from .rebuild import Rebuilder
from .scanner import scan
from .transcoder import TranscodeConfig, transcode_segment
from .ui import Reporter

_DEFAULT_DB_NAME = "tubetape.json"


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
        default=18,
        help="video re-encode quality; lower is better (default: 18)",
    )
    parser.add_argument(
        "--max-resolution",
        type=durations.parse_resolution,
        default="3840x2160",
        help="max target resolution WxH, never upscaling (default: 3840x2160)",
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
        "--no-rebuild",
        action="store_true",
        help="do not rebuild sealed segments; make supplemental segments instead",
    )
    parser.add_argument(
        "--rebuild-cooldown",
        type=durations.parse_duration,
        default="24h",
        help="min interval between two rebuilds of the same segment (default: 24h)",
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
        help="in watch mode, poll for new files every this many seconds (default: 30s)",
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

    return args


def _segment_params(args: argparse.Namespace) -> tuple:
    return (args.image_duration, args.crf, args.max_resolution, args.ken_burns)


def run_pipeline(args: argparse.Namespace, reporter: Reporter | None = None) -> int:
    reporter = reporter or Reporter()
    db = Database.load(args.db)

    reporter.status(f"scanning {args.input} ...")
    result = scan(
        args.input,
        db,
        args.timezone,
        args.image_duration,
        only_camera_photos=args.only_camera_photos,
        only_phone_videos=args.only_phone_videos,
    )
    for error in result.errors:
        reporter.status(f"  error: {error['path']}: {error['reason']}")
    for item in result.skipped:
        reporter.status(f"  skipped: {item['path']} ({item['reason']})")

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
        for item in plan_result.pending_files:
            reporter.status(f"  pending file {item.file_id[:12]} {item.rel_path}")
        return 0

    # Real run: persist scanned files, then transcode and upload.
    for item in result.files:
        db.upsert_file(item.file_id, item.to_record())
    for file_id in result.deleted_file_ids:
        db.remove_file(file_id)

    # Real run: transcode each new/rebuild segment, then upload.
    uploader = _build_uploader(args, db)
    if uploader is None:
        reporter.status("no YouTube credentials; set TUBETAPE_TOKEN or token.json")
        db.save()
        return 1

    config = TranscodeConfig(
        crf=args.crf,
        max_resolution=args.max_resolution,
        ken_burns=args.ken_burns,
        image_duration=args.image_duration,
    )

    for segment in plan_result.segments:
        if not uploader.quota.can_upload():
            reporter.status(
                f"quota exhausted ({uploader.quota.used}/{uploader.quota.daily_limit}); "
                f"{len(plan_result.segments)} segment(s) deferred to next run"
            )
            break
        segment_files = [files_by_id[fid] for fid in segment.file_ids if fid in files_by_id]
        out_path = os.path.join(os.path.dirname(args.db), f".{segment.segment_id[:12]}.mp4")

        def transcode_fn(files, _out_path=out_path):
            return transcode_segment(files, _out_path, config)

        reporter.status(f"transcoding {segment.title} ...")
        if segment.is_rebuild:
            reporter.status(
                f"rebuilding {segment.title} (replaces {segment.replaces_segment_id[:12]} ...) ..."
            )
            rebuilder = Rebuilder(
                db,
                transcode_fn=transcode_fn,
                upload_fn=lambda out, title, desc: uploader.upload(
                    out, title, desc, privacy=args.privacy
                ),
                verify_fn=uploader.verify,
                delete_fn=uploader.delete_video,
                quota=uploader.quota,
            )
            video_id = rebuilder.rebuild(
                segment.replaces_segment_id,
                segment,
                segment_files,
                title=segment.title,
            )
        else:
            _, chapters = transcode_fn(segment_files)
            reporter.status(f"uploading {segment.title} ...")
            video_id = uploader.upload(
                out_path,
                segment.title,
                chapters_text(chapters),
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
                    "status": "sealed",
                    "chapters": chapters,
                    "last_rebuilt_at": None,
                    "attempts": 0,
                    "error": None,
                },
            )

        # Clean up the local transcode output now that it's uploaded.
        try:
            os.remove(out_path)
        except OSError:
            pass

        db.settings["quota"] = uploader.quota.to_dict()
        db.save()
        reporter.quota(uploader.quota.used, uploader.quota.daily_limit)

    db.save()
    reporter.status("done")
    return 0


def _media_paths(input_dir: str) -> set[str]:
    import os

    from .scanner import MEDIA_EXTENSIONS

    paths: set[str] = set()
    for root, dirs, names in os.walk(input_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() in MEDIA_EXTENSIONS:
                paths.add(os.path.join(root, name))
    return paths


def run_watch(args: argparse.Namespace, reporter: Reporter | None = None) -> int:
    """Process once, then keep re-processing as new media files appear."""
    import time

    reporter = reporter or Reporter()

    code = run_pipeline(args, reporter)
    if code != 0:
        return code

    reporter.status(
        f"watching for new files (poll {args.poll_interval:g}s, "
        f"quiet {args.quiet_period:g}s, Ctrl+C to stop) ..."
    )
    known = _media_paths(args.input)
    last_change = time.time()
    has_changes = False

    from datetime import datetime, timezone

    last_day = datetime.now(timezone.utc).date()

    try:
        while True:
            time.sleep(max(1.0, args.poll_interval))

            # Daily re-process: quota rolls over each day, so re-run once a day
            # to upload segments deferred by quota exhaustion.
            today = datetime.now(timezone.utc).date()
            if today != last_day:
                last_day = today
                reporter.status("new day (quota reset); re-processing ...")
                run_pipeline(args, reporter)
                known = _media_paths(args.input)
                has_changes = False
                last_change = time.time()
                continue

            current = _media_paths(args.input)
            new = current - known
            if new:
                reporter.status(f"detected {len(new)} new file(s)")
                known = current
                last_change = time.time()
                has_changes = True
            if has_changes and time.time() - last_change >= args.quiet_period:
                reporter.status("quiet period elapsed; re-processing ...")
                run_pipeline(args, reporter)
                known = _media_paths(args.input)
                has_changes = False
    except KeyboardInterrupt:
        reporter.status("exit signal: flushing pending segments ...")
        args.flush = True
        run_pipeline(args, reporter)
    return 0


def _build_uploader(args: argparse.Namespace, db: Database):
    import os

    from .uploader import QuotaTracker, Uploader

    token_str = os.environ.get(auth.TOKEN_ENV)
    if not token_str:
        token_path = os.path.join(os.path.dirname(args.db), "token.json")
        if not os.path.exists(token_path):
            return None
        credentials = auth.load_token_file(token_path)
    else:
        credentials = auth.credentials_from_token_string(token_str)

    quota = QuotaTracker.from_dict(db.settings.get("quota"))
    quota.rollover()

    from googleapiclient.discovery import build

    service = build("youtube", "v3", credentials=credentials)
    return Uploader(service, quota=quota, playlist_id=getattr(args, "playlist", None))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.watch and not args.dry_run:
        return run_watch(args)
    return run_pipeline(args)


if __name__ == "__main__":
    sys.exit(main())
