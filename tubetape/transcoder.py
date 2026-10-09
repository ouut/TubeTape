"""Video transcoding with ffmpeg.

Every intermediate clip is re-encoded to identical H.264/AAC parameters and a
uniform canvas (scale-to-fit then letterbox), then the clips are concatenated
with stream copy to avoid a second quality loss.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageOps

from .chapters import build_chapters
from .log import get_logger
from .planner import display_ts
from .scanner import (
    HEIF_EXTENSIONS,
    FILE_TYPE_IMAGE,
    FILE_TYPE_VIDEO,
    ScannedFile,
)

_logger = get_logger("transcoder")


@dataclass
class TranscodeConfig:
    crf: int = 16
    max_resolution: tuple[int, int] = (7680, 4320)
    canvas_mode: str = "max"  # "max" (bounding box) | "first"
    ken_burns: bool = False
    image_duration: float = 3.0
    fps: int = 60
    preset: str = "slow"


def target_resolution(
    orig_w: int, orig_h: int, max_w: int, max_h: int
) -> tuple[int, int]:
    """Scale a resolution down to fit within ``max_w x max_h``, never upscaling.

    Dimensions are made even (required by H.264 yuv420p).
    """
    if orig_w <= max_w and orig_h <= max_h:
        return orig_w, orig_h
    scale = min(max_w / orig_w, max_h / orig_h)
    width = int(orig_w * scale)
    height = int(orig_h * scale)
    return (width - width % 2, height - height % 2)


def _parse_resolution(resolution: str | None) -> tuple[int, int] | None:
    if not resolution:
        return None
    try:
        width, height = resolution.lower().split("x", 1)
        return int(width), int(height)
    except (ValueError, AttributeError):
        return None


def segment_canvas(files: list[ScannedFile], config: TranscodeConfig) -> tuple[int, int]:
    """Pick the segment's uniform canvas resolution.

    ``max`` (default): the bounding box of all files' (capped) sizes, so no
    file is downscaled. ``first``: the first file's (capped) size.
    """
    sizes: list[tuple[int, int]] = []
    for item in files:
        parsed = _parse_resolution(item.resolution)
        if parsed is not None:
            sizes.append(target_resolution(parsed[0], parsed[1], *config.max_resolution))
    if not sizes:
        return config.max_resolution
    if config.canvas_mode == "first":
        return sizes[0]
    width = max(w for w, _ in sizes)
    height = max(h for _, h in sizes)
    return width, height


def _scale_pad_filter(canvas_w: int, canvas_h: int) -> str:
    return (
        f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,"
        f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )


def _ken_burns_filter(canvas_w: int, canvas_h: int, duration: float, fps: int) -> str:
    frames = max(1, int(duration * fps))
    return (
        f"scale={canvas_w * 2}:{canvas_h * 2}:force_original_aspect_ratio=increase,"
        f"crop={canvas_w}:{canvas_h},"
        f"zoompan=z='min(zoom+0.0015,1.25)':d={frames}:s={canvas_w}x{canvas_h}:fps={fps}"
    )


def _image_filter(canvas_w: int, canvas_h: int, config: TranscodeConfig) -> str:
    if config.ken_burns:
        return _ken_burns_filter(canvas_w, canvas_h, config.image_duration, config.fps)
    return _scale_pad_filter(canvas_w, canvas_h)


def build_image_clip_command(
    src: str, dst: str, canvas_w: int, canvas_h: int, duration: float, config: TranscodeConfig
) -> list[str]:
    vf = _image_filter(canvas_w, canvas_h, config)
    return [
        "ffmpeg", "-y",
        "-loop", "1", "-i", src,
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-vf", vf,
        "-t", str(duration), "-r", str(config.fps),
        "-c:v", "libx264", "-preset", config.preset, "-crf", str(config.crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest",
        dst,
    ]


def build_video_clip_command(
    src: str, dst: str, canvas_w: int, canvas_h: int, config: TranscodeConfig
) -> list[str]:
    vf = _scale_pad_filter(canvas_w, canvas_h)
    return [
        "ffmpeg", "-y",
        "-i", src,
        "-vf", vf, "-r", str(config.fps),
        "-c:v", "libx264", "-preset", config.preset, "-crf", str(config.crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
        dst,
    ]


def build_concat_command(list_path: str, dst: str) -> list[str]:
    return ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", dst]


def run_ffmpeg(cmd: list[str]) -> None:
    _logger.debug("ffmpeg command: %s", " ".join(cmd))
    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        _logger.error("ffmpeg command failed after %.1fs: %s", time.monotonic() - start, exc)
        raise RuntimeError(f"ffmpeg failed: {exc}") from exc
    if proc.returncode != 0:
        _logger.error(
            "ffmpeg exited with code %d after %.1fs: %s",
            proc.returncode,
            time.monotonic() - start,
            proc.stderr[-2000:],
        )
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {proc.stderr[-2000:]}")
    _logger.debug("ffmpeg finished successfully in %.1fs", time.monotonic() - start)


def check_disk_space(path: str, required_bytes: int) -> None:
    stat = os.statvfs(path)
    free = stat.f_bavail * stat.f_frsize
    if free < required_bytes:
        raise OSError(f"insufficient disk space: {free} < {required_bytes} bytes")


def _heif_to_png(src: str, dst: str) -> None:
    """Convert a HEIC/HEIF image to PNG for ffmpeg (which usually can't decode HEIF).

    Applies the EXIF orientation so portrait photos aren't rotated in the video.
    """
    with Image.open(src) as img:
        img = ImageOps.exif_transpose(img)
        img.convert("RGB").save(dst, "PNG")


def transcode_segment(
    files: list[ScannedFile],
    out_path: str,
    config: TranscodeConfig | None = None,
    work_dir: str | None = None,
    progress: Callable[[int, int, ScannedFile], None] | None = None,
) -> tuple[str, list[list[str]]]:
    """Transcode a segment's files into a single MP4 with resumable staging.

    Returns ``(out_path, chapters)`` where chapters are ``[[start, title], ...]``.

    ``progress``, when given, is called before each clip as
    ``progress(done, total, item)`` (``done`` is 1-based).
    """
    config = config or TranscodeConfig()
    canvas_w, canvas_h = segment_canvas(files, config)
    _logger.info(
        "transcoding segment: %d file(s) -> %s (canvas %dx%d)",
        len(files),
        out_path,
        canvas_w,
        canvas_h,
    )

    chapters = build_chapters(
        [
            (item.duration_seconds or config.image_duration, display_ts(item.captured_epoch or 0))
            for item in files
        ],
        min_gap=0.0,
    )
    _logger.info("built %d chapter(s) from %d file(s)", len(chapters), len(files))

    out_abs = os.path.abspath(out_path)
    out_dir = os.path.dirname(out_abs)
    out_base = os.path.splitext(os.path.basename(out_abs))[0]
    os.makedirs(out_dir, exist_ok=True)

    # 1. Level 1: If the final segment video is ALREADY fully transcoded and assembled on disk,
    # directly reuse it without doing any transcode or concat work!
    if os.path.isfile(out_abs) and os.path.getsize(out_abs) > 1024:
        _logger.info(
            "target segment video already exists on disk: %s (%.1f MB), skipping transcode entirely",
            out_abs,
            os.path.getsize(out_abs) / (1024 * 1024),
        )
        if progress is not None and files:
            try:
                progress(len(files), len(files), files[-1], True)  # type: ignore[call-arg]
            except TypeError:
                progress(len(files), len(files), files[-1])
        return out_path, chapters

    # Check if a matching video exists under the same segment short_id [sid16]
    if "[" in out_base and "]" in out_base and os.path.isdir(out_dir):
        sid_tag = out_base[out_base.rfind("[") : out_base.rfind("]") + 1]
        if len(sid_tag) > 8:
            for entry in os.scandir(out_dir):
                if entry.is_file() and entry.name.lower().endswith(".mp4") and not entry.name.startswith(".") and sid_tag in entry.name:
                    if entry.stat().st_size > 1024:
                        _logger.info(
                            "target segment video found under alternate name %s (%.1f MB), reusing",
                            entry.path,
                            entry.stat().st_size / (1024 * 1024),
                        )
                        if progress is not None and files:
                            try:
                                progress(len(files), len(files), files[-1], True)  # type: ignore[call-arg]
                            except TypeError:
                                progress(len(files), len(files), files[-1])
                        return entry.path, chapters

    staging_parent = os.path.abspath(work_dir) if work_dir else out_dir
    staging_dir = os.path.join(staging_parent, f".staging_{out_base}")
    # If exact staging dir does not exist, look for any staging dir matching [sid16]
    if not os.path.isdir(staging_dir) and "[" in out_base and "]" in out_base and os.path.isdir(staging_parent):
        sid_tag = out_base[out_base.rfind("[") : out_base.rfind("]") + 1]
        if len(sid_tag) > 8:
            for entry in os.scandir(staging_parent):
                if entry.is_dir() and entry.name.startswith(".staging_") and sid_tag in entry.name:
                    staging_dir = entry.path
                    _logger.info("reusing existing staging directory %s matching %s", staging_dir, sid_tag)
                    break
    os.makedirs(staging_dir, exist_ok=True)

    clips: list[str] = []
    for index, item in enumerate(files):
        clip_name = f"clip_{index:04d}_{item.file_id[:12]}.mp4"
        clip_path = os.path.join(staging_dir, clip_name)
        tmp_clip_path = os.path.join(staging_dir, f"clip_{index:04d}_{item.file_id[:12]}.tmp.mp4")

        # Resume check: if clip already exists and has valid size (> 1024 bytes), reuse it!
        if os.path.isfile(clip_path) and os.path.getsize(clip_path) > 1024:
            _logger.info(
                "resuming clip %d/%d: %s (%s) already transcoded, reusing",
                index + 1,
                len(files),
                item.rel_path,
                clip_name,
            )
            if progress is not None:
                try:
                    progress(index + 1, len(files), item, True)  # type: ignore[call-arg]
                except TypeError:
                    progress(index + 1, len(files), item)
            clips.append(clip_path)
            continue

        if progress is not None:
            try:
                progress(index + 1, len(files), item, False)  # type: ignore[call-arg]
            except TypeError:
                progress(index + 1, len(files), item)

        if os.path.isfile(tmp_clip_path):
            try:
                os.remove(tmp_clip_path)
            except OSError:
                pass

        if item.type == FILE_TYPE_IMAGE:
            img_src = item.abs_path
            if os.path.splitext(item.abs_path)[1].lower() in HEIF_EXTENSIONS:
                img_src = os.path.join(staging_dir, f"src_{index:04d}_{item.file_id[:12]}.png")
                if not os.path.isfile(img_src) or os.path.getsize(img_src) == 0:
                    _logger.info(
                        "converting HEIF image %s to PNG for ffmpeg",
                        item.rel_path,
                    )
                    _heif_to_png(item.abs_path, img_src)
            cmd = build_image_clip_command(
                img_src, tmp_clip_path, canvas_w, canvas_h,
                item.duration_seconds or config.image_duration, config,
            )
        elif item.type == FILE_TYPE_VIDEO:
            cmd = build_video_clip_command(item.abs_path, tmp_clip_path, canvas_w, canvas_h, config)
        else:
            raise ValueError(f"unknown file type: {item.type!r}")

        _logger.info("clip %d/%d: %s (%s)", index + 1, len(files), item.rel_path, item.type)
        run_ffmpeg(cmd)
        os.replace(tmp_clip_path, clip_path)
        clips.append(clip_path)

    list_path = os.path.join(staging_dir, "concat.txt")
    with open(list_path, "w", encoding="utf-8") as handle:
        for clip in clips:
            handle.write(f"file '{clip}'\n")
    _logger.debug("concatenating %d clip(s)", len(clips))

    tmp_out = out_abs + ".tmp.mp4"
    if os.path.isfile(tmp_out):
        try:
            os.remove(tmp_out)
        except OSError:
            pass

    run_ffmpeg(build_concat_command(list_path, tmp_out))
    os.replace(tmp_out, out_abs)

    shutil.rmtree(staging_dir, ignore_errors=True)
    _logger.info("transcode complete: %s (staging cleaned up)", out_path)
    return out_path, chapters
