"""Video transcoding with ffmpeg.

Every intermediate clip is re-encoded to identical H.264/AAC parameters and a
uniform canvas (scale-to-fit then letterbox), then the clips are concatenated
with stream copy to avoid a second quality loss.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass

from .chapters import build_chapters
from .planner import display_ts
from .scanner import ScannedFile, FILE_TYPE_IMAGE, FILE_TYPE_VIDEO


@dataclass
class TranscodeConfig:
    crf: int = 18
    max_resolution: tuple[int, int] = (3840, 2160)
    ken_burns: bool = False
    image_duration: float = 3.0
    fps: int = 30
    preset: str = "veryfast"


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
    """Pick the segment's uniform canvas resolution (first file's target size)."""
    for item in files:
        parsed = _parse_resolution(item.resolution)
        if parsed is not None:
            return target_resolution(parsed[0], parsed[1], *config.max_resolution)
    return config.max_resolution


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
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"ffmpeg failed: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {proc.stderr[-2000:]}")


def check_disk_space(path: str, required_bytes: int) -> None:
    stat = os.statvfs(path)
    free = stat.f_bavail * stat.f_frsize
    if free < required_bytes:
        raise OSError(f"insufficient disk space: {free} < {required_bytes} bytes")


def transcode_segment(
    files: list[ScannedFile],
    out_path: str,
    config: TranscodeConfig | None = None,
    work_dir: str | None = None,
) -> tuple[str, list[list[str]]]:
    """Transcode a segment's files into a single MP4.

    Returns ``(out_path, chapters)`` where chapters are ``[[start, title], ...]``.
    """
    config = config or TranscodeConfig()
    canvas_w, canvas_h = segment_canvas(files, config)

    chapters = build_chapters(
        [
            (item.duration_seconds or config.image_duration, display_ts(item.captured_epoch or 0))
            for item in files
        ]
    )

    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        clips: list[str] = []
        for index, item in enumerate(files):
            clip_path = os.path.join(tmp, f"clip_{index:04d}.mp4")
            if item.type == FILE_TYPE_IMAGE:
                cmd = build_image_clip_command(
                    item.abs_path, clip_path, canvas_w, canvas_h,
                    item.duration_seconds or config.image_duration, config,
                )
            elif item.type == FILE_TYPE_VIDEO:
                cmd = build_video_clip_command(item.abs_path, clip_path, canvas_w, canvas_h, config)
            else:
                raise ValueError(f"unknown file type: {item.type!r}")
            run_ffmpeg(cmd)
            clips.append(clip_path)

        list_path = os.path.join(tmp, "concat.txt")
        with open(list_path, "w", encoding="utf-8") as handle:
            for clip in clips:
                handle.write(f"file '{clip}'\n")

        run_ffmpeg(build_concat_command(list_path, out_path))

    return out_path, chapters
