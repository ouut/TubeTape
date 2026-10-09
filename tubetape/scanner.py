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

# HEIF/HEIC support needs pillow-heif; registering it lets Pillow open .heic/.heif.
try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
except Exception:  # noqa: BLE001 - optional; .heic then fails to open and is reported
    _logger.debug("pillow-heif not available; .heic/.heif images cannot be read")

IMAGE_EXTENSIONS = frozenset(
    [".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp", ".bmp"]
)
VIDEO_EXTENSIONS = frozenset(
    [".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts", ".3gp"]
)
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

# HEIF-family containers: decoded via Pillow, converted to PNG before ffmpeg,
# because ffmpeg builds commonly lack a HEIC/HEIF decoder.
HEIF_EXTENSIONS = frozenset([".heic", ".heif"])

# Formats that actually carry EXIF metadata (skip exifread for the rest).
EXIF_CAPABLE_EXTENSIONS = frozenset(
    [".jpg", ".jpeg", ".tif", ".tiff", ".heic", ".heif"]
)

# Content hashing samples the head, middle and tail of each file (plus its
# size) instead of reading it end to end. This turns the first scan from
# O(total bytes) into O(number of files) and is what makes large libraries
# fast. Files smaller than 3 chunks are read in full. Raise HASH_CHUNK to
# trade a little speed for a lower (already very small) chance of sampling
# collisions.
HASH_CHUNK = 64 * 1024  # bytes sampled from head, middle and tail

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


def file_sha256(path: str, chunk: int = HASH_CHUNK) -> str:
    """Return the content hash of a file.

    Hashes the head/middle/tail ``chunk`` bytes plus the file size, so huge
    files are not read end to end on every scan. Files smaller than three
    chunks are read in full (sampling would cover them anyway).
    """
    size = os.path.getsize(path)
    hasher = hashlib.sha256()

    if size <= 3 * chunk:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                hasher.update(block)
        _logger.debug("hashed %s (%d bytes, full hash)", path, size)
        return hasher.hexdigest()

    with open(path, "rb") as handle:
        hasher.update(handle.read(chunk))  # head
        handle.seek(max(0, size // 2 - chunk // 2))
        hasher.update(handle.read(chunk))  # middle
        handle.seek(max(0, size - chunk))
        hasher.update(handle.read(chunk))  # tail
    hasher.update(str(size).encode("ascii"))
    _logger.debug(
        "hashed %s (%d bytes, sampled head/middle/tail %dKB + size)",
        path,
        size,
        chunk // 1024,
    )
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


def _pillow_heif_gps(exif) -> dict | None:
    """Read GPS from a Pillow EXIF object (HEIC/HEIF)."""
    try:
        gps = exif.get_ifd(0x8825)  # GPSInfo IFD
    except Exception:  # noqa: BLE001
        return None
    if not gps:
        return None
    lat_ref, lat = gps.get(1), gps.get(2)
    lng_ref, lng = gps.get(3), gps.get(4)
    if not lat or not lng:
        return None
    try:
        lat_val = _apply_gps_ref(_dms_to_decimal(lat), str(lat_ref), "S", "N")
        lng_val = _apply_gps_ref(_dms_to_decimal(lng), str(lng_ref), "W", "E")
    except (AttributeError, TypeError, ValueError):
        return None
    if lat_val is None or lng_val is None:
        return None
    return {"lat": round(lat_val, 6), "lng": round(lng_val, 6)}


def _pillow_heif_exif(exif, tz) -> dict:
    """Extract capture time / make / model / GPS from a Pillow EXIF object.

    Used for HEIC/HEIF, whose metadata ``exifread`` cannot parse.
    """
    captured_at_utc = None
    captured_epoch = None
    missing_meta = True
    make = model = software = None
    location = None
    try:
        exif_ifd = exif.get_ifd(0x8769)  # ExifIFD
        dt_original = (
            exif_ifd.get(0x9003)  # DateTimeOriginal
            or exif_ifd.get(0x9004)  # DateTimeDigitized
            or exif.get(0x0132)  # DateTime
        )
        if dt_original:
            dt = parse_exif_datetime(str(dt_original), tz)
            if dt is not None:
                captured_at_utc = format_iso_utc(dt)
                captured_epoch = dt.timestamp()
                missing_meta = False
        make = exif.get(0x010F)
        model = exif.get(0x0110)
        software = exif.get(0x0131)
        location = _pillow_heif_gps(exif)
    except Exception:  # noqa: BLE001 - EXIF is best-effort
        pass
    return {
        "captured_at_utc": captured_at_utc,
        "captured_epoch": captured_epoch,
        "missing_meta": missing_meta,
        "location": location,
        "make": str(make) if make else None,
        "model": str(model) if model else None,
        "software": str(software) if software else None,
    }


def _exifread_exif(path: str, tz) -> dict:
    """Extract capture time / make / model / GPS via exifread (JPEG/TIFF)."""
    captured_at_utc = None
    captured_epoch = None
    missing_meta = True
    location = None
    make = model = software = None
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
    except Exception:  # noqa: BLE001 - EXIF is best-effort
        pass
    return {
        "captured_at_utc": captured_at_utc,
        "captured_epoch": captured_epoch,
        "missing_meta": missing_meta,
        "location": location,
        "make": make,
        "model": model,
        "software": software,
    }


def _image_meta(path: str, tz) -> dict:
    ext = os.path.splitext(path)[1].lower()
    with Image.open(path) as img:
        resolution = f"{img.width}x{img.height}"
        pillow_exif = None
        if ext in HEIF_EXTENSIONS:
            try:
                pillow_exif = img.getexif()
            except Exception:  # noqa: BLE001 - best-effort
                pillow_exif = None

    if ext not in EXIF_CAPABLE_EXTENSIONS:
        info = {
            "captured_at_utc": None,
            "captured_epoch": None,
            "missing_meta": True,
            "location": None,
            "make": None,
            "model": None,
            "software": None,
        }
    elif pillow_exif is not None:
        info = _pillow_heif_exif(pillow_exif, tz)
    else:
        info = _exifread_exif(path, tz)

    return {
        "resolution": resolution,
        "duration_seconds": None,
        **info,
        "is_camera": bool(info["make"]) and bool(info["model"]),
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
    mtime_ns: int | None = None  # retained for backward compatibility

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
        }

    @classmethod
    def from_record(cls, file_id: str, record: dict, input_dir: str = ".") -> ScannedFile:
        rel_path = record.get("path") or ""
        abs_path = os.path.abspath(os.path.join(input_dir, rel_path)) if input_dir else rel_path
        cap_utc = record.get("captured_at_utc")
        cap_epoch = record.get("captured_epoch")
        if cap_epoch is None and cap_utc:
            dt = parse_iso_utc(cap_utc)
            if dt:
                cap_epoch = dt.timestamp()
        return cls(
            file_id=file_id,
            abs_path=abs_path,
            rel_path=rel_path,
            name=record.get("name") or os.path.basename(rel_path),
            type=record.get("type", "image"),
            size_bytes=int(record.get("size_bytes", 0)),
            captured_at_utc=cap_utc,
            captured_epoch=cap_epoch,
            resolution=record.get("resolution"),
            duration_seconds=float(record.get("duration_seconds") or 0.0) if record.get("duration_seconds") is not None else None,
            location=record.get("location"),
            missing_meta=bool(record.get("missing_meta", False)),
            time_source=record.get("time_source", "metadata"),
            source=record.get("source", "unknown"),
            mtime_ns=record.get("mtime_ns"),
        )



@dataclass
class ScanResult:
    files: list[ScannedFile] = field(default_factory=list)
    new_file_ids: list[str] = field(default_factory=list)
    processed_file_ids: list[str] = field(default_factory=list)
    deleted_file_ids: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)  # filtered-out files


# ------------------------------------------------------------------- scanner


def collect_media_files(input_dir: str) -> list[tuple[str, str, str, str]]:
    """Recursively collect (abs_path, rel_path, name, ftype) for all supported media files."""
    input_dir = os.path.abspath(input_dir)
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
    return candidates


def scan_single_file(
    abs_path: str,
    rel_path: str,
    name: str,
    ftype: str,
    timezone,
    image_duration: float = 3.0,
    hash_chunk: int = HASH_CHUNK,
) -> ScannedFile:
    """Read a single media file, compute its sampled SHA-256 hash, and extract metadata."""
    st = os.stat(abs_path)
    size = st.st_size
    file_id = file_sha256(abs_path, hash_chunk)

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
    return ScannedFile(
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
    )


def scan_files(
    input_dir: str,
    timezone,
    image_duration: float = 3.0,
    hash_chunk: int = HASH_CHUNK,
    only_camera_photos: bool = False,
    only_phone_videos: bool = False,
    cache: dict[str, dict] | None = None,  # retained for backwards compatibility
    on_file: Callable[[ScannedFile], None] | None = None,
    candidates: list[tuple[str, str, str, str]] | None = None,
) -> tuple[list[ScannedFile], list[dict], list[dict]]:
    """Scan media files, extracting metadata and computing IDs.

    Unparseable/corrupt files go into ``errors``; files excluded by the
    camera/phone filters go into ``skipped``.

    ``on_file``, when given, is called with each successfully-scanned file
    so the caller can persist progress incrementally.
    """
    input_dir = os.path.abspath(input_dir)
    scanned: list[ScannedFile] = []
    errors: list[dict] = []
    skipped: list[dict] = []

    if candidates is None:
        _logger.info("scanning directory: %s", input_dir)
        candidates = collect_media_files(input_dir)

    total = len(candidates)
    _logger.info("found %d media file(s) to scan", total)

    for index, (abs_path, rel_path, name, ftype) in enumerate(candidates, start=1):
        started = time.monotonic()
        try:
            item = scan_single_file(
                abs_path=abs_path,
                rel_path=rel_path,
                name=name,
                ftype=ftype,
                timezone=timezone,
                image_duration=image_duration,
                hash_chunk=hash_chunk,
            )
            _logger.info(
                "scan [%d/%d] %s (%s, %.1f MB)",
                index,
                total,
                rel_path,
                ftype,
                item.size_bytes / (1024 * 1024),
            )
            _logger.debug(
                "scanned %s in %.1fs: id=%s captured=%s source=%s time_source=%s",
                rel_path,
                time.monotonic() - started,
                item.file_id[:12],
                item.captured_at_utc,
                item.source,
                item.time_source,
            )
        except Exception as exc:  # noqa: BLE001 - skip unparseable files
            errors.append({"path": rel_path, "reason": str(exc), "ts": utc_now_iso()})
            _logger.warning("failed to scan %s: %s", rel_path, exc)
            continue

        # Source filters apply to scanned files.
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
    force_scan: bool = False,
    on_file: Callable[[ScannedFile], None] | None = None,
) -> ScanResult:
    """Scan and reconcile against the database using path sets and precise incremental loading.

    - When force_scan=True: re-scans all media files on disk.
    - When disk_paths == db_paths: skips scanning entirely and restores files from database.
    - When disk_paths != db_paths: precisely scans only new files, removes deleted files,
      and retains unchanged files directly from database without re-reading them.
    """
    input_dir = os.path.abspath(input_dir)
    candidates = collect_media_files(input_dir)
    disk_paths = {c[1] for c in candidates}
    candidate_map = {c[1]: c for c in candidates}

    # Map relative path -> file_id from db
    db_path_to_id: dict[str, str] = {}
    for fid, record in db.files.items():
        p = record.get("path")
        if p:
            db_path_to_id[p] = fid

    db_paths = set(db_path_to_id.keys())
    existing_ids = set(db.files.keys())

    # Case 1: Force full scan
    if force_scan:
        _logger.info("force-scan enabled: rescanning all %d media files", len(candidates))
        scanned, errors, skipped = scan_files(
            input_dir,
            timezone,
            image_duration,
            only_camera_photos=only_camera_photos,
            only_phone_videos=only_phone_videos,
            on_file=on_file,
            candidates=candidates,
        )
        scanned_ids = {f.file_id for f in scanned}
        new_file_ids = [f.file_id for f in scanned if f.file_id not in existing_ids]
        processed_file_ids = [f.file_id for f in scanned if f.file_id in existing_ids]
        deleted_file_ids = sorted(existing_ids - scanned_ids)
        return ScanResult(
            files=scanned,
            new_file_ids=new_file_ids,
            processed_file_ids=processed_file_ids,
            deleted_file_ids=deleted_file_ids,
            errors=errors,
            skipped=skipped,
        )

    # Helper function to filter retained records
    def _restore_retained(paths: set[str]) -> tuple[list[ScannedFile], list[dict]]:
        retained: list[ScannedFile] = []
        filter_skipped: list[dict] = []
        for rel_p in sorted(paths):
            fid = db_path_to_id[rel_p]
            rec = db.files[fid]
            item = ScannedFile.from_record(fid, rec, input_dir)
            if item.type == FILE_TYPE_IMAGE and only_camera_photos and item.source != "camera":
                filter_skipped.append({"path": item.rel_path, "reason": "not a camera photo"})
                continue
            if item.type == FILE_TYPE_VIDEO and only_phone_videos and item.source != "camera":
                filter_skipped.append({"path": item.rel_path, "reason": "not a phone video"})
                continue
            retained.append(item)
        return retained, filter_skipped

    # Case 2: Identical path sets -> skip scanning completely
    if disk_paths == db_paths:
        _logger.info("media file paths match database index (%d files); skipping scan", len(disk_paths))
        retained_files, skipped = _restore_retained(disk_paths)
        return ScanResult(
            files=retained_files,
            new_file_ids=[],
            processed_file_ids=[f.file_id for f in retained_files],
            deleted_file_ids=[],
            errors=[],
            skipped=skipped,
        )

    # Case 3: Precise incremental scan (方案 A)
    added_paths = disk_paths - db_paths
    deleted_paths = db_paths - disk_paths
    retained_paths = disk_paths & db_paths

    _logger.info(
        "precise incremental scan: %d added, %d deleted, %d retained",
        len(added_paths),
        len(deleted_paths),
        len(retained_paths),
    )

    retained_files, skipped = _restore_retained(retained_paths)

    # Scan only the newly added candidate files
    added_candidates = [candidate_map[p] for p in sorted(added_paths)]
    newly_scanned, errors, added_skipped = scan_files(
        input_dir,
        timezone,
        image_duration,
        only_camera_photos=only_camera_photos,
        only_phone_videos=only_phone_videos,
        on_file=on_file,
        candidates=added_candidates,
    )
    skipped.extend(added_skipped)

    # Deleted files: IDs in existing_ids whose paths are not on disk
    deleted_file_ids = sorted({fid for fid, rec in db.files.items() if rec.get("path") not in disk_paths})

    all_files = retained_files + newly_scanned
    new_file_ids = [f.file_id for f in newly_scanned]
    processed_file_ids = [f.file_id for f in retained_files]

    return ScanResult(
        files=all_files,
        new_file_ids=new_file_ids,
        processed_file_ids=processed_file_ids,
        deleted_file_ids=deleted_file_ids,
        errors=errors,
        skipped=skipped,
    )

