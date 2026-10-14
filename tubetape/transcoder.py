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
import threading
import time
from collections import deque
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
    crf: int = 18
    max_resolution: tuple[int, int] = (3840, 2160)
    canvas_mode: str = "max"  # "max" (bounding box) | "first"
    ken_burns: bool = False
    image_duration: float = 3.0
    fps: int = 30
    preset: str = "fast"
    encoder: str = "auto"


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
        f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
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


_PROBED_ENCODERS: dict[str, bool] = {}
_DETECTED_ENCODER: str | None = None


def probe_encoder(name: str) -> bool:
    """Test if a given ffmpeg encoder can actually encode a 1-frame test clip."""
    if name in _PROBED_ENCODERS:
        return _PROBED_ENCODERS[name]

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=s=64x64:d=0.04",
        "-c:v", name,
        "-f", "null", "-",
    ]
    try:
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2.0)
        ok = (res.returncode == 0)
    except Exception:
        ok = False
    _PROBED_ENCODERS[name] = ok
    return ok


def detect_best_encoder() -> str:
    """Probe ffmpeg for the best available H.264 video encoder.

    Checks in priority order:
    1. h264_nvenc (NVIDIA GPU hardware acceleration)
    2. h264_videotoolbox (Apple Silicon / Mac GPU hardware acceleration)
    3. h264_qsv (Intel QuickSync hardware acceleration)
    4. libx264 (Universal CPU software fallback)
    """
    global _DETECTED_ENCODER
    if _DETECTED_ENCODER is not None:
        return _DETECTED_ENCODER

    for name in ("h264_nvenc", "h264_videotoolbox", "h264_qsv"):
        if probe_encoder(name):
            _logger.info("hardware video encoder detected and verified: %s", name)
            _DETECTED_ENCODER = name
            return name

    _logger.info("no hardware video encoder detected; using CPU libx264")
    _DETECTED_ENCODER = "libx264"
    return "libx264"


def resolve_encoder(requested: str | None) -> str:
    """Resolve requested encoder name, auto-detecting if 'auto' or empty."""
    if not requested or requested.lower() == "auto":
        return detect_best_encoder()
    req = requested.lower().strip()
    if not probe_encoder(req):
        best = detect_best_encoder()
        _logger.warning("requested encoder %r is not supported on this system; falling back to %s", req, best)
        return best
    return req


def get_encoder_flags(encoder: str, config: TranscodeConfig, is_still: bool = False) -> list[str]:
    """Generate ffmpeg command-line flags for the chosen encoder and configuration."""
    enc = resolve_encoder(encoder)
    if enc == "h264_nvenc":
        # NVENC: supports -preset (p1-p7 or fast/medium/slow), -cq:v, -rc:v vbr
        preset = config.preset if config.preset in ("p1", "p2", "p3", "p4", "p5", "p6", "p7", "fast", "medium", "slow") else "fast"
        return [
            "-c:v", "h264_nvenc",
            "-preset", preset,
            "-rc:v", "vbr",
            "-cq:v", str(config.crf),
            "-pix_fmt", "yuv420p",
        ]
    elif enc == "h264_videotoolbox":
        # VideoToolbox: -q:v (1-100 scale, ~70-75 corresponds to crf 18-20)
        vt_q = max(30, min(95, int(100 - config.crf * 1.5)))
        return [
            "-c:v", "h264_videotoolbox",
            "-q:v", str(vt_q),
            "-pix_fmt", "yuv420p",
        ]
    elif enc == "h264_qsv":
        # Intel QuickSync
        return [
            "-c:v", "h264_qsv",
            "-global_quality", str(config.crf),
            "-preset", config.preset,
            "-pix_fmt", "nv12",
        ]
    else:
        # libx264 CPU fallback
        args = ["-c:v", "libx264", "-preset", config.preset, "-crf", str(config.crf)]
        if is_still:
            args.extend(["-tune", "stillimage"])
        args.extend(["-pix_fmt", "yuv420p"])
        return args


def build_image_clip_command(
    src: str, dst: str, canvas_w: int, canvas_h: int, duration: float, config: TranscodeConfig
) -> list[str]:
    vf = _image_filter(canvas_w, canvas_h, config)
    enc_flags = get_encoder_flags(config.encoder, config, is_still=True)
    return [
        "ffmpeg", "-y",
        "-loop", "1", "-i", src,
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-vf", vf,
        "-t", str(duration), "-r", str(config.fps),
        *enc_flags,
        "-c:a", "aac", "-b:a", "128k", "-shortest",
        dst,
    ]


def build_video_clip_command(
    src: str, dst: str, canvas_w: int, canvas_h: int, config: TranscodeConfig
) -> list[str]:
    vf = _scale_pad_filter(canvas_w, canvas_h)
    enc_flags = get_encoder_flags(config.encoder, config, is_still=False)
    return [
        "ffmpeg", "-y",
        "-i", src,
        "-vf", vf, "-r", str(config.fps),
        *enc_flags,
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
        dst,
    ]


def build_photos_concat_command(
    list_path: str, dst: str, canvas_w: int, canvas_h: int, config: TranscodeConfig
) -> list[str]:
    vf = (
        f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,"
        f"fps={config.fps},format=yuv420p"
    )
    gop = max(1, config.fps // 2)
    enc_flags = get_encoder_flags(config.encoder, config, is_still=True)
    return [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", list_path,
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-vf", vf,
        *enc_flags,
        "-bf", "2", "-g", str(gop), "-movflags", "+faststart",
        "-c:a", "aac", "-b:a", "128k", "-shortest",
        dst,
    ]


def build_concat_command(list_path: str, dst: str) -> list[str]:
    return ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", dst]


def _format_clock(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "00:00"
    m = int(seconds // 60)
    s = int(seconds % 60)
    if m >= 60:
        h = m // 60
        m = m % 60
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def run_ffmpeg(
    cmd: list[str],
    total_duration: float | None = None,
    desc: str | None = None,
    on_progress: Callable[[float, float, float, float, str], None] | None = None,
    log_interval: float = 15.0,
    timeout: float = 3600.0,
) -> None:
    _logger.debug("ffmpeg command: %s", " ".join(cmd))
    start = time.monotonic()

    # Inject -progress pipe:1 if not present
    run_cmd = list(cmd)
    if "-progress" not in run_cmd:
        if len(run_cmd) >= 2 and run_cmd[1] == "-y":
            run_cmd[2:2] = ["-progress", "pipe:1"]
        else:
            run_cmd[1:1] = ["-progress", "pipe:1"]

    try:
        proc = subprocess.Popen(
            run_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        _logger.error("ffmpeg not found: %s", exc)
        raise RuntimeError(f"ffmpeg not found: {exc}") from exc

    stderr_lines = deque(maxlen=60)

    def _drain_stderr():
        try:
            if proc.stderr:
                for line in proc.stderr:
                    stderr_lines.append(line)
        except Exception:
            pass

    err_thread = threading.Thread(target=_drain_stderr, daemon=True)
    err_thread.start()

    last_log_time = start
    cur_fps = 0.0
    cur_speed = "N/A"
    cur_sec = 0.0

    try:
        if proc.stdout:
            for line in proc.stdout:
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k == "fps":
                    try:
                        cur_fps = float(v)
                    except ValueError:
                        pass
                elif k == "speed":
                    cur_speed = v.strip()
                elif k == "out_time_us":
                    try:
                        cur_sec = max(0.0, int(v) / 1_000_000.0)
                    except ValueError:
                        pass
                elif k == "progress":
                    now = time.monotonic()
                    if v == "continue" and now - last_log_time >= log_interval:
                        last_log_time = now
                        pct = (
                            min(99.9, max(0.0, (cur_sec / total_duration) * 100.0))
                            if total_duration and total_duration > 0
                            else 0.0
                        )
                        cur_clk = _format_clock(cur_sec)
                        tot_clk = _format_clock(total_duration) if total_duration else "--:--"
                        pct_text = f"{pct:.1f}%" if (total_duration and total_duration > 0) else cur_clk
                        _logger.info(
                            "[转码中] %s: %s (已编码 %s / 总长 %s) | fps=%.1f | 速度=%s",
                            desc or "视频处理",
                            pct_text,
                            cur_clk,
                            tot_clk,
                            cur_fps,
                            cur_speed,
                        )
                        if on_progress:
                            try:
                                on_progress(pct, cur_sec, total_duration or 0.0, cur_fps, cur_speed)
                            except Exception:
                                pass
        proc.wait(timeout=max(60.0, timeout))
    except (subprocess.TimeoutExpired, Exception) as exc:
        proc.kill()
        _logger.error("ffmpeg command failed or timed out after %.1fs: %s", time.monotonic() - start, exc)
        raise RuntimeError(f"ffmpeg failed: {exc}") from exc
    finally:
        err_thread.join(timeout=1.0)

    duration = time.monotonic() - start
    if proc.returncode != 0:
        err_msg = "".join(stderr_lines)[-2000:]
        _logger.error(
            "ffmpeg exited with code %d after %.1fs: %s",
            proc.returncode,
            duration,
            err_msg,
        )
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {err_msg}")

    _logger.debug("ffmpeg finished successfully in %.1fs", duration)


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

    # 0. Single-video segment: do not re-encode! Copy directly without quality loss.
    if len(files) == 1 and files[0].type == FILE_TYPE_VIDEO:
        src_path = os.path.abspath(files[0].abs_path)
        if src_path != out_abs:
            if not os.path.isfile(out_abs) or os.path.getsize(out_abs) != os.path.getsize(src_path):
                _logger.info("single video segment: copying %s -> %s without re-encoding", src_path, out_abs)
                shutil.copy2(src_path, out_abs)
            else:
                _logger.info("single video segment already present at %s, reusing", out_abs)
        if progress is not None:
            try:
                progress(1, 1, files[0], True)
            except TypeError:
                pass
        return out_path, chapters

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

    # 2. Pure photo album fast-path using concat demuxer (single-pass encode)
    if all(item.type == FILE_TYPE_IMAGE for item in files) and not config.ken_burns:
        images_txt = os.path.join(staging_dir, "images.txt")
        cum_durations: list[float] = []
        cur_cum = 0.0
        with open(images_txt, "w", encoding="utf-8") as handle:
            for index, item in enumerate(files):
                img_src = os.path.abspath(item.abs_path)
                if os.path.splitext(item.abs_path)[1].lower() in HEIF_EXTENSIONS:
                    img_src = os.path.join(staging_dir, f"src_{index:04d}_{item.file_id[:12]}.png")
                    if not os.path.isfile(img_src) or os.path.getsize(img_src) == 0:
                        _logger.info("converting HEIF image %s to PNG for ffmpeg", item.rel_path)
                        _heif_to_png(item.abs_path, img_src)
                dur = item.duration_seconds or config.image_duration
                cur_cum += dur
                cum_durations.append(cur_cum)
                escaped = img_src.replace("'", "'\\''")
                handle.write(f"file '{escaped}'\n")
                handle.write(f"duration {dur}\n")
            if files:
                last_item = files[-1]
                last_src = os.path.abspath(last_item.abs_path)
                if os.path.splitext(last_item.abs_path)[1].lower() in HEIF_EXTENSIONS:
                    last_src = os.path.join(staging_dir, f"src_{len(files)-1:04d}_{last_item.file_id[:12]}.png")
                escaped_last = last_src.replace("'", "'\\''")
                handle.write(f"file '{escaped_last}'\n")

        tmp_out = out_abs + ".tmp.mp4"
        if os.path.isfile(tmp_out):
            try:
                os.remove(tmp_out)
            except OSError:
                pass

        total_dur = cum_durations[-1] if cum_durations else 0.0
        cmd = build_photos_concat_command(images_txt, tmp_out, canvas_w, canvas_h, config)

        def _on_photos_prog(pct: float, cur_s: float, tot_s: float, fps: float, speed: str) -> None:
            if progress is not None and files:
                cur_idx = 0
                for idx, c_dur in enumerate(cum_durations):
                    if cur_s <= c_dur:
                        cur_idx = idx
                        break
                else:
                    cur_idx = len(files) - 1
                item = files[cur_idx]
                try:
                    progress(
                        cur_idx + 1,
                        len(files),
                        item,
                        False,
                        file_progress=pct,
                        fps=fps,
                        speed=speed,
                    )
                except TypeError:
                    try:
                        progress(cur_idx + 1, len(files), item, False)
                    except TypeError:
                        progress(cur_idx + 1, len(files), item)

        _logger.info("rendering photo album with concat demuxer: %d photo(s) -> %s", len(files), out_path)
        try:
            run_ffmpeg(
                cmd,
                total_duration=total_dur,
                desc=f"photos {out_base}",
                on_progress=_on_photos_prog,
                log_interval=10.0,
            )
        except TypeError as te:
            if "unexpected keyword argument" in str(te):
                run_ffmpeg(cmd)
            else:
                raise

        os.replace(tmp_out, out_abs)
        shutil.rmtree(staging_dir, ignore_errors=True)
        _logger.info("photo album transcode complete: %s (staging cleaned up)", out_path)
        return out_path, chapters

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
            clip_dur = item.duration_seconds or config.image_duration
            cmd = build_image_clip_command(
                img_src, tmp_clip_path, canvas_w, canvas_h,
                clip_dur, config,
            )
        elif item.type == FILE_TYPE_VIDEO:
            clip_dur = item.duration_seconds
            cmd = build_video_clip_command(item.abs_path, tmp_clip_path, canvas_w, canvas_h, config)
        else:
            raise ValueError(f"unknown file type: {item.type!r}")

        _logger.info("clip %d/%d: %s (%s)", index + 1, len(files), item.rel_path, item.type)

        def _on_clip_prog(pct: float, cur_s: float, tot_s: float, fps: float, speed: str) -> None:
            if progress is not None:
                try:
                    progress(
                        index + 1,
                        len(files),
                        item,
                        False,
                        file_progress=pct,
                        fps=fps,
                        speed=speed,
                    )
                except TypeError:
                    try:
                        progress(index + 1, len(files), item, False)
                    except TypeError:
                        progress(index + 1, len(files), item)

        try:
            run_ffmpeg(
                cmd,
                total_duration=clip_dur,
                desc=f"clip {index + 1}/{len(files)} ({item.name})",
                on_progress=_on_clip_prog,
                log_interval=15.0,
            )
        except TypeError as te:
            if "unexpected keyword argument" in str(te):
                run_ffmpeg(cmd)
            else:
                raise
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

    total_concat_dur = sum(
        (item.duration_seconds or config.image_duration) if item.type == FILE_TYPE_IMAGE else (item.duration_seconds or 0.0)
        for item in files
    )
    _logger.info("concatenating %d clip(s) into %s ...", len(clips), out_path)
    try:
        run_ffmpeg(
            build_concat_command(list_path, tmp_out),
            total_duration=total_concat_dur,
            desc=f"concat {out_base}",
            log_interval=10.0,
        )
    except TypeError as te:
        if "unexpected keyword argument" in str(te):
            run_ffmpeg(build_concat_command(list_path, tmp_out))
        else:
            raise
    os.replace(tmp_out, out_abs)

    shutil.rmtree(staging_dir, ignore_errors=True)
    _logger.info("transcode complete: %s (staging cleaned up)", out_path)
    return out_path, chapters
