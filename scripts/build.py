#!/usr/bin/env python3
"""Build a standalone TubeTape executable with PyInstaller.

Builds for the CURRENT operating system only (PyInstaller does not
cross-compile). To produce binaries for all three platforms at once, push to
GitHub and let the workflow in .github/workflows/build.yml build Linux/Windows/
macOS via CI.

Usage:
    python scripts/build.py            # build for this OS
    python scripts/build.py --name tube  # custom output name

Requirements:
    pip install pyinstaller
    ffmpeg must be installed on the target machine (it is not bundled).

Output lands in dist/.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from pathlib import Path


def _ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller not found. Install it with: pip install pyinstaller", file=sys.stderr)
        raise SystemExit(2)


def build(name: str) -> None:
    root = Path(__file__).resolve().parent.parent
    entry = root / "tubetape" / "__main__.py"

    # --collect-all bundles package data files (e.g. tzdata timezone db,
    # googleapiclient discovery docs).
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", name,
        "--clean",
        "--noconfirm",
        "--collect-all", "tzdata",
        "--collect-all", "googleapiclient",
        str(entry),
    ]

    print(f"building {name} for {platform.system()} ({platform.machine()}) ...")
    subprocess.run(cmd, cwd=str(root), check=True)

    exe = name + (".exe" if platform.system() == "Windows" else "")
    out = root / "dist" / exe
    print(f"\nbuilt: {out}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="tubetape", help="output executable name")
    args = parser.parse_args(argv)

    _ensure_pyinstaller()
    build(args.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
