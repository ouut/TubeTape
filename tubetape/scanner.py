"""Filesystem scanner: discover media files, extract metadata, compute IDs.

Capture-time priority (per prompt.md, extended by M8):
  1. video: QuickTime/MOV ``creation_time`` metadata (prefer UTC);
  2. photo: EXIF ``DateTimeOriginal`` (timezone-less, interpreted via --timezone);
  3. filename timestamp pattern (e.g. WeChat/QQ ``YYYY_MM_DD_HH_MM_*``);
  4. fallback: file mtime, marking ``missing_meta: true``.

``file_id`` is the SHA-256 of file content; large files use a fast hash of the
first/middle/last 4 MB plus size.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

import exifread
from PIL import Image

from .db import FILE_TYPE_IMAGE, FILE_TYPE_VIDEO, Database
from .log import get_logger

_logger = get_logger("scanner")

IMAGE_EXTENSIONS = frozenset(
    [".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp", ".bmp"]
)
VIDEO_EXTENSIONS = frozenset(
    [".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts"]
)
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# Formats that actually carry EXIF metadata (skip exifread for the rest).
EXIF_CAPABLE_EXTENSIONS = frozenset([".jpg", ".jpeg", ".tif", ".tiff", ".heic"])

FAST_HASH_THRESHOLD = 512 * 1024 * 1024  # 512 MB
FAST_HASH_CHUNK = 4 * 1024 * 1024  # 4 MB

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_RESOLUTION_RE = re.compile(r"(\d{2,5})x(\d{2,5})")
_CREATION_TIME_RE = re.compile(r"creation_time\s*:\s*(\S+)")
# Camera/phone manufacturer in video metadata (iPhone/Android).
_MAKE_RE = re.compile(
    r"^\s*(?:com\.apple\.quicktime\.make|com\.android\.manufacturer|make)\s*:\s*(.+?)\s*$",
    re.MULTILINE,
)

_EXIF_DATETIME_FORMATS = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S")

# Common filename timestamp layouts. Each pattern is searched (not anchored),
# so prefixes like "IMG_" or "VID_" are fine.
_FILENAME_TIME_PATTERNS = [
    re.compile(r"(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})(?:_(\d{2}))?"),  # 2020_12_16_01_00[_59]_...
    re.compile(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})"),  # 20201216_010000
    re.compile(r"(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(?!\d)"),  # 20201216_0100
    re.compile(r"(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})"),  # 2020-12-16 01-00-00
]
_ISO_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
)


# ---------------------------------------------------------------- timestamps


def format_iso_utc(dt: datetime) -> str:
    """Format a datetime as ISO 8601 UTC ``YYYY-MM-DDTHH:MM:SSZ``."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_iso() -> str:
    return format_iso_utc(datetime.now(timezone.utc))


def parse_iso_utc(text: str) -> datetime | None:
    """Parse an ISO-ish timestamp into a UTC-aware datetime.

    Handles a trailing ``Z`` (as ``+00:00``), numeric offsets, and naive
    timestamps (assumed UTC).
    """
    text = str(text).strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        dt = None
        for fmt in _ISO_FORMATS:
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_exif_datetime(text: str, tz) -> datetime | None:
    """Parse an EXIF DateTimeOriginal string, interpreting it in ``tz``."""
    text = str(text).strip()
    for fmt in _EXIF_DATETIME_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(timezone.utc)


def parse_filename_time(name: str, tz) -> datetime | None:
    """Extract a capture time from a filename, interpreting it in ``tz``.

    Returns a UTC-aware datetime, or None when no recognizable timestamp is
    found (or the timestamp is out of range).
    """
    text = os.path.splitext(str(name))[0]
    for pattern in _FILENAME_TIME_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groups()
        if groups and groups[-1] is None:
            groups = groups[:-1]  # optional seconds absent
        if len(groups) < 5:
            continue
        try:
            year, month, day, hour, minute = (int(g) for g in groups[:5])
            second = int(groups[5]) if len(groups) > 5 else 0
            dt = datetime(year, month, day, hour, minute, second)
        except (ValueError, TypeError):
            continue  # out-of-range date (e.g. month 13)
        return dt.replace(tzinfo=tz).astimezone(timezone.utc)
    return None


# ------------------------------------------------------------------- hashing


def file_sha256(path: str, threshold: int = FAST_HASH_THRESHOLD) -> str:
    """Return the content hash of a file.

    Files larger than ``threshold`` bytes use a fast hash: first/middle/last
    4 MB plus the file size, so re-hashing huge files stays cheap.
    """
    size = os.path.getsize(path)
    hasher = hashlib.sha256()
    if size <= threshold:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(chunk)
        _logger.debug("hashed %s (%d bytes, full hash)", path, size)
        return hasher.hexdigest()

    _logger.debug("hashed %s (%d bytes, fast hash: first/middle/last 4MB + size)", path, size)
    with open(path, "rb") as handle:
        hasher.update(handle.read(FAST_HASH_CHUNK))  # first 4 MB
        handle.seek(max(0, size // 2 - FAST_HASH_CHUNK // 2))
        hasher.update(handle.read(FAST_HASH_CHUNK))  # middle 4 MB
        handle.seek(max(0, size - FAST_HASH_CHUNK))
        hasher.update(handle.read(FAST_HASH_CHUNK))  # last 4 MB
    hasher.update(str(size).encode("ascii"))
    return hasher.hexdigest()


# ------------------------------------------------------------- ffmpeg probing


def _parse_ffmpeg_duration(stderr: str) -> float | None:
    match = _DURATION_RE.search(stderr)
    if not match:
        return None
    hours, minutes, seconds = (float(g) for g in match.groups())
    return hours * 3600.0 + minutes * 60.0 + seconds


def _parse_ffmpeg_resolution(stderr: str) -> str | None:
    for line in stderr.splitlines():
        if "Video:" in line:
            match = _RESOLUTION_RE.search(line)
            if match:
                return f"{match.group(1)}x{match.group(2)}"
    return None


def _parse_ffmpeg_creation_time(stderr: str) -> str | None:
    match = _CREATION_TIME_RE.search(stderr)
    return match.group(1) if match else None


def _parse_ffmpeg_make(stderr: str) -> str | None:
    match = _MAKE_RE.search(stderr)
    return match.group(1).strip() if match else None


def _parse_ffprobe_json(text: str) -> dict:
    """Parse ``ffprobe -print_format json`` output into the probe dict."""
    data = json.loads(text)

    fmt = data.get("format") or {}
    tags = fmt.get("tags") or {}
    duration = None
    if fmt.get("duration"):
        try:
            duration = float(fmt["duration"])
        except (TypeError, ValueError):
            duration = None

    creation_time = tags.get("creation_time")
    make = _make_from_tags(tags)

    resolution = None
    for stream in data.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        width, height = stream.get("width"), stream.get("height")
        if width and height:
            resolution = f"{width}x{height}"
        # creation_time / make may also live on the video stream's tags.
        stags = stream.get("tags") or {}
        if not creation_time:
            creation_time = stags.get("creation_time")
        if not make:
            make = _make_from_tags(stags)
        break

    return {
        "duration_seconds": duration,
        "resolution": resolution,
        "creation_time": creation_time,
        "make": make,
    }


def _make_from_tags(tags: dict) -> str | None:
    """Pick a camera manufacturer out of common ffprobe tag names."""
    return (
        tags.get("make")
        or tags.get("com.apple.quicktime.make")
        or tags.get("com.android.manufacturer")
    )


def probe_video(path: str) -> dict:
    """Probe a video, preferring ``ffprobe`` (structured, faster).

    Falls back to ``ffmpeg -i`` stderr parsing when ffprobe is missing or
    returns nothing usable (e.g. no duration).
    """
    _logger.debug("probing video metadata: %s", path)

    if shutil.which("ffprobe"):
        try:
            proc = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json",
                 "-show_format", "-show_streams", path],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                result = _parse_ffprobe_json(proc.stdout)
                if result["duration_seconds"] is not None:
                    _logger.debug(
                        "probed %s via ffprobe: duration=%s resolution=%s "
                        "creation_time=%s make=%s",
                        path,
                        result["duration_seconds"],
                        result["resolution"],
                        result["creation_time"],
                        result["make"],
                    )
                    return result
        except (json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
            _logger.debug("ffprobe failed for %s (%s); falling back to ffmpeg", path, exc)

    # Fallback: ffmpeg -i and regex parsing of stderr.
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", path],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _logger.warning("ffmpeg probe failed for %s: %s", path, exc)
        raise ValueError(f"ffmpeg probe failed: {exc}") from exc

    stderr = proc.stderr or ""
    result = {
        "duration_seconds": _parse_ffmpeg_duration(stderr),
        "resolution": _parse_ffmpeg_resolution(stderr),
        "creation_time": _parse_ffmpeg_creation_time(stderr),
        "make": _parse_ffmpeg_make(stderr),
    }
    _logger.debug(
        "probed %s via ffmpeg: duration=%s resolution=%s creation_time=%s make=%s",
        path,
        result["duration_seconds"],
        result["resolution"],
        result["creation_time"],
        result["make"],
    )
    return result


# ---------------------------------------------------------------- GPS helpers


def _dms_to_decimal(parts) -> float:
    """Convert degrees/minutes/seconds (rationals or floats) to decimal degrees."""
    values = []
    for part in parts:
        if hasattr(part, "num") and hasattr(part, "den"):
            values.append(float(part.num) / float(part.den))
        else:
            values.append(float(part))
    degrees, minutes, seconds = values
    return degrees + minutes / 60.0 + seconds / 3600.0


def _apply_gps_ref(value: float, ref: str, neg: str, pos: str) -> float | None:
    ref = str(ref).strip().upper()
    if ref == neg:
        return -value
    if ref == pos:
        return value
    return None


def _exifread_gps(tags) -> dict | None:
    lat = tags.get("GPS GPSLatitude")
    lat_ref = tags.get("GPS GPSLatitudeRef")
    lng = tags.get("GPS GPSLongitude")
    lng_ref = tags.get("GPS GPSLongitudeRef")
    if not lat or not lng:
        return None
    try:
        lat_val = _apply_gps_ref(_dms_to_decimal(lat.values), str(lat_ref), "S", "N")
        lng_val = _apply_gps_ref(_dms_to_decimal(lng.values), str(lng_ref), "W", "E")
    except (AttributeError, TypeError, ValueError):
        return None
    if lat_val is None or lng_val is None:
        return None
    return {"lat": round(lat_val, 6), "lng": round(lng_val, 6)}


# ------------------------------------------------------------- metadata read


def _image_meta(path: str, tz) -> dict:
    with Image.open(path) as img:
        resolution = f"{img.width}x{img.height}"

    captured_at_utc = None
    captured_epoch = None
    missing_meta = True
    location = None
    make = None
    model = None
    software = None

    ext = os.path.splitext(path)[1].lower()
    if ext not in EXIF_CAPABLE_EXTENSIONS:
        return {
            "resolution": resolution,
            "duration_seconds": None,
            "captured_at_utc": None,
            "captured_epoch": None,
            "location": None,
            "missing_meta": True,
            "make": None,
            "model": None,
            "software": None,
            "is_camera": False,
        }

    try:
        with open(path, "rb") as handle:
            tags = exifread.process_file(handle, details=False)
        dt_original = tags.get("EXIF DateTimeOriginal")
        if dt_original is not None:
            dt = parse_exif_datetime(str(dt_original), tz)
            if dt is not None:
                captured_at_utc = format_iso_utc(dt)
                captured_epoch = dt.timestamp()
                missing_meta = False
        location = _exifread_gps(tags)
        make = str(tags["Image Make"]) if tags.get("Image Make") else None
        model = str(tags["Image Model"]) if tags.get("Image Model") else None
        software = str(tags["Image Software"]) if tags.get("Image Software") else None
    except Exception:
        # EXIF is best-effort; fall back to mtime below.
        pass

    return {
        "resolution": resolution,
        "duration_seconds": None,
        "captured_at_utc": captured_at_utc,
        "captured_epoch": captured_epoch,
        "location": location,
        "missing_meta": missing_meta,
        "make": make,
        "model": model,
        "software": software,
        "is_camera": bool(make) and bool(model),
    }


def _video_meta(path: str) -> dict:
    info = probe_video(path)
    if info["duration_seconds"] is None:
        raise ValueError("unreadable video (no duration)")

    captured_at_utc = None
    captured_epoch = None
    missing_meta = True
    if info["creation_time"]:
        dt = parse_iso_utc(info["creation_time"])
        if dt is not None:
            captured_at_utc = format_iso_utc(dt)
            captured_epoch = dt.timestamp()
            missing_meta = False

    return {
        "resolution": info["resolution"],
        "duration_seconds": info["duration_seconds"],
        "captured_at_utc": captured_at_utc,
        "captured_epoch": captured_epoch,
        "location": None,
        "missing_meta": missing_meta,
        "make": info["make"],
        "model": None,
        "software": None,
        "is_camera": bool(info["make"]),
    }


def _mtime_utc(path: str) -> tuple[str, float]:
    epoch = os.path.getmtime(path)
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return format_iso_utc(dt), epoch


# --------------------------------------------------------------------- models


@dataclass
class ScannedFile:
    file_id: str
    abs_path: str
    rel_path: str
    name: str
    type: str
    size_bytes: int
    captured_at_utc: str | None = None
    captured_epoch: float | None = None
    resolution: str | None = None
    duration_seconds: float | None = None
    location: dict | None = None
    missing_meta: bool = False
    time_source: str = "metadata"  # metadata | filename | mtime
    source: str = "unknown"  # camera | other (camera photo / phone video vs the rest)
    mtime_ns: int | None = None  # st_mtime_ns, used for the scan cache

    def to_record(self) -> dict:
        return {
            "path": self.rel_path,
            "name": self.name,
            "type": self.type,
            "captured_at_utc": self.captured_at_utc,
            "resolution": self.resolution,
            "duration_seconds": self.duration_seconds,
            "size_bytes": self.size_bytes,
            "sha256": self.file_id,
            "location": self.location,
            "missing_meta": self.missing_meta,
            "time_source": self.time_source,
            "source": self.source,
            "mtime_ns": self.mtime_ns,
        }


@dataclass
class ScanResult:
    files: list[ScannedFile] = field(default_factory=list)
    new_file_ids: list[str] = field(default_factory=list)
    processed_file_ids: list[str] = field(default_factory=list)
    deleted_file_ids: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # filtered-out files


# ------------------------------------------------------------------- scanner


def scan_files(
    input_dir: str,
    timezone,
    image_duration: float = 3.0,
    hash_threshold: int = FAST_HASH_THRESHOLD,
    only_camera_photos: bool = False,
    only_phone_videos: bool = False,
    cache: dict[str, dict] | None = None,
    on_file: Callable[[ScannedFile], None] | None = None,
) -> tuple[list[ScannedFile], list[dict], list[dict]]:
    """Recursively scan ``input_dir``, returning (scanned files, errors, skipped).

    Unparseable/corrupt files go into ``errors``; files excluded by the
    camera/phone filters go into ``skipped``.

    ``cache`` maps ``rel_path`` to a previous database record. When a file's
    size and mtime both match its cached record, the expensive content hash and
    metadata probing (ffprobe/EXIF) are skipped and the cached values reused.

    ``on_file``, when given, is called with each successfully-scanned file
    (including cache hits) so the caller can persist progress incrementally.
    """
    input_dir = os.path.abspath(input_dir)
    scanned: list[ScannedFile] = []
    errors: list[dict] = []
    skipped: list[dict] = []

    _logger.info("scanning directory: %s", input_dir)

    # Phase 1: collect candidate media files. This is fast (only walks names),
    # so we can report the total count up front and show per-file progress
    # during the slow hashing/metadata phase below.
    candidates: list[tuple[str, str, str, str]] = []
    for root, dirs, names in os.walk(input_dir):
        # Skip hidden files/dirs: the transcode output is a dotfile written
        # next to the db, and must not be re-scanned as media.
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in sorted(names):
            if name.startswith("."):
                continue
            abs_path = os.path.join(root, name)
            ext = os.path.splitext(name)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                ftype = FILE_TYPE_IMAGE
            elif ext in VIDEO_EXTENSIONS:
                ftype = FILE_TYPE_VIDEO
            else:
                continue
            rel_path = os.path.relpath(abs_path, input_dir)
            candidates.append((abs_path, rel_path, name, ftype))

    total = len(candidates)
    _logger.info("found %d media file(s) to scan", total)

    # Phase 2: hash + extract metadata for each candidate, one at a time, with
    # a progress line *before* each file so a slow/large file is always named.
    # Unchanged files (same size + mtime as a previous record) skip the hash
    # and ffprobe/EXIF steps entirely via the record cache.
    for index, (abs_path, rel_path, name, ftype) in enumerate(candidates, start=1):
        try:
            st = os.stat(abs_path)
            size = st.st_size
            mtime_ns = st.st_mtime_ns
        except OSError as exc:
            errors.append({"path": rel_path, "reason": str(exc), "ts": utc_now_iso()})
            _logger.warning("failed to stat %s: %s", rel_path, exc)
            continue

        cached = (cache or {}).get(rel_path)
        cache_hit = bool(
            cached
            and cached.get("sha256")
            and cached.get("type") == ftype
            and cached.get("size_bytes") == size
            and cached.get("mtime_ns") == mtime_ns
        )

        _logger.info(
            "scan [%d/%d] %s (%s, %.1f MB)%s",
            index,
            total,
            rel_path,
            ftype,
            size / (1024 * 1024),
            " [cache]" if cache_hit else "",
        )

        if cache_hit:
            captured_at_utc = cached.get("captured_at_utc")
            captured_epoch = None
            if captured_at_utc:
                dt = parse_iso_utc(captured_at_utc)
                if dt is not None:
                    captured_epoch = dt.timestamp()
            item = ScannedFile(
                file_id=cached["sha256"],
                abs_path=abs_path,
                rel_path=rel_path,
                name=name,
                type=ftype,
                size_bytes=size,
                captured_at_utc=captured_at_utc,
                captured_epoch=captured_epoch,
                resolution=cached.get("resolution"),
                duration_seconds=cached.get("duration_seconds"),
                location=cached.get("location"),
                missing_meta=bool(cached.get("missing_meta", True)),
                time_source=cached.get("time_source", "metadata"),
                source=cached.get("source", "unknown"),
                mtime_ns=mtime_ns,
            )
        else:
            started = time.monotonic()
            try:
                file_id = file_sha256(abs_path, hash_threshold)
                if ftype == FILE_TYPE_IMAGE:
                    meta = _image_meta(abs_path, timezone)
                    duration = image_duration
                else:
                    meta = _video_meta(abs_path)
                    duration = meta["duration_seconds"]

                captured_at_utc = meta["captured_at_utc"]
                captured_epoch = meta["captured_epoch"]
                missing_meta = meta["missing_meta"]
                time_source = "metadata"
                if captured_at_utc is None:
                    filename_time = parse_filename_time(name, timezone)
                    if filename_time is not None:
                        captured_at_utc = format_iso_utc(filename_time)
                        captured_epoch = filename_time.timestamp()
                        time_source = "filename"
                    else:
                        captured_at_utc, captured_epoch = _mtime_utc(abs_path)
                        time_source = "mtime"
                    missing_meta = True

                source = "camera" if meta["is_camera"] else "other"
                item = ScannedFile(
                    file_id=file_id,
                    abs_path=abs_path,
                    rel_path=rel_path,
                    name=name,
                    type=ftype,
                    size_bytes=size,
                    captured_at_utc=captured_at_utc,
                    captured_epoch=captured_epoch,
                    resolution=meta["resolution"],
                    duration_seconds=duration,
                    location=meta["location"],
                    missing_meta=missing_meta,
                    time_source=time_source,
                    source=source,
                    mtime_ns=mtime_ns,
                )
                _logger.debug(
                    "scanned %s in %.1fs: id=%s captured=%s source=%s time_source=%s",
                    rel_path,
                    time.monotonic() - started,
                    file_id[:12],
                    captured_at_utc,
                    source,
                    time_source,
                )
            except Exception as exc:  # noqa: BLE001 - skip unparseable files
                errors.append({"path": rel_path, "reason": str(exc), "ts": utc_now_iso()})
                _logger.warning("failed to scan %s: %s", rel_path, exc)
                continue

        # Source filters apply to both cached and freshly-scanned files.
        if ftype == FILE_TYPE_IMAGE and only_camera_photos and item.source != "camera":
            skipped.append({"path": rel_path, "reason": "not a camera photo"})
            _logger.info("skipped %s (not a camera photo)", rel_path)
            continue
        if ftype == FILE_TYPE_VIDEO and only_phone_videos and item.source != "camera":
            skipped.append({"path": rel_path, "reason": "not a phone video"})
            _logger.info("skipped %s (not a phone video)", rel_path)
            continue

        scanned.append(item)
        if on_file is not None:
            on_file(item)

    _logger.info(
        "directory scan finished: %d scanned, %d error(s), %d skipped",
        len(scanned),
        len(errors),
        len(skipped),
    )
    return scanned, errors, skipped


def scan(
    input_dir: str,
    db: Database,
    timezone,
    image_duration: float = 3.0,
    only_camera_photos: bool = False,
    only_phone_videos: bool = False,
    on_file: Callable[[ScannedFile], None] | None = None,
) -> ScanResult:
    """Scan and reconcile against the database (new / processed / deleted)."""
    cache: dict[str, dict] = {}
    existing_ids = set(db.files.keys())  # snapshot before any incremental upserts
    for record in db.files.values():
        path = record.get("path")
        if path:
            cache[path] = record
    _logger.debug("built scan cache with %d cached path(s)", len(cache))

    scanned, errors, skipped = scan_files(
        input_dir,
        timezone,
        image_duration,
        only_camera_photos=only_camera_photos,
        only_phone_videos=only_phone_videos,
        cache=cache,
        on_file=on_file,
    )
    scanned_ids = {f.file_id for f in scanned}

    new_file_ids = [f.file_id for f in scanned if f.file_id not in existing_ids]
    processed_file_ids = [f.file_id for f in scanned if f.file_id in existing_ids]
    deleted_file_ids = sorted(existing_ids - scanned_ids)

    _logger.info(
        "reconciled against db: %d new, %d processed, %d deleted",
        len(new_file_ids),
        len(processed_file_ids),
        len(deleted_file_ids),
    )

    return ScanResult(
        files=scanned,
        new_file_ids=new_file_ids,
        processed_file_ids=processed_file_ids,
        deleted_file_ids=deleted_file_ids,
        errors=errors,
        skipped=skipped,
    )
