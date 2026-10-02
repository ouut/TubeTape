"""Shared test fixtures for generating small media samples."""

from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image


def make_jpeg_with_exif(path: Path, dt_str: str = "2024:01:01 15:30:00") -> None:
    """Write a small JPEG whose EXIF DateTimeOriginal is ``dt_str``."""
    img = Image.new("RGB", (100, 50), "red")
    exif = Image.Exif()
    exif_ifd = exif.get_ifd(0x8769)  # ExifIFD
    exif_ifd[0x9003] = dt_str  # DateTimeOriginal
    img.save(path, exif=exif)


def make_camera_photo(
    path: Path,
    make: str = "Apple",
    model: str = "iPhone 13",
    dt_str: str = "2024:01:01 15:30:00",
) -> None:
    """Write a JPEG with camera EXIF (Make + Model + DateTimeOriginal)."""
    img = Image.new("RGB", (100, 80), "red")
    exif = Image.Exif()
    exif[0x010F] = make  # Make
    exif[0x0110] = model  # Model
    exif_ifd = exif.get_ifd(0x8769)  # ExifIFD
    exif_ifd[0x9003] = dt_str  # DateTimeOriginal
    img.save(path, exif=exif)


def make_heic(
    path: Path,
    make: str = "Apple",
    model: str = "iPhone 13",
    dt_str: str = "2024:01:01 15:30:00",
    size: tuple[int, int] = (100, 80),
) -> None:
    """Write a small HEIC image with camera EXIF (Make + Model + DateTimeOriginal)."""
    import pillow_heif

    pillow_heif.register_heif_opener()
    img = Image.new("RGB", size, "red")
    exif = img.getexif()
    exif[0x010F] = make  # Make
    exif[0x0110] = model  # Model
    exif_ifd = exif.get_ifd(0x8769)  # ExifIFD
    exif_ifd[0x9003] = dt_str  # DateTimeOriginal
    img.save(path, format="HEIF", exif=exif)


def make_phone_video(
    path: Path,
    make: str = "Apple",
    model: str = "iPhone 13",
    duration: int = 2,
) -> None:
    """Write a MOV with phone camera metadata (make + model)."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s=320x240:d={duration}",
        "-movflags", "use_metadata_tags",
        "-metadata", f"make={make}",
        "-metadata", f"model={model}",
        "-metadata", "creation_time=2024-01-01T15:30:00Z",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def make_png(path: Path, size: tuple[int, int] = (80, 40)) -> None:
    Image.new("RGB", size, "blue").save(path)


def make_video(
    path: Path,
    creation_time: str | None = "2024-01-01T15:30:00Z",
    duration: int = 2,
    size: str = "320x240",
) -> None:
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={size}:d={duration}",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
    ]
    if creation_time is not None:
        cmd += ["-metadata", f"creation_time={creation_time}"]
    cmd += [
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def make_corrupt(path: Path) -> None:
    path.write_bytes(b"this is definitely not a valid image or video")
