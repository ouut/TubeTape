#!/usr/bin/env python3
"""Upload a single video to YouTube, for safely testing the upload path.

Usage:
    TUBETAPE_TOKEN='<token-json>' python scripts/upload_one.py \\
        video.mp4 "My Test Title" "0:00 chapter" --privacy private

    # or with a token.json file instead of the env var:
    python scripts/upload_one.py video.mp4 "title" --token-file /path/token.json

This bypasses scan/plan/transcode and exercises only auth + videos.insert +
quota, so you can validate metadata/章节/配额 against a real private channel
before running the full pipeline.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from googleapiclient.discovery import build

from tubetape import auth
from tubetape.uploader import Uploader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", help="path to the video file to upload")
    parser.add_argument("title", help="video title")
    parser.add_argument("description", nargs="?", default="", help="description (chapters text)")
    parser.add_argument("--privacy", default="private", choices=["private", "unlisted"])
    parser.add_argument("--playlist", default=None, help="optional playlist ID to append to")
    parser.add_argument("--token-file", default=None, help="path to token.json (else TUBETAPE_TOKEN env)")
    args = parser.parse_args(argv)

    token_str = os.environ.get(auth.TOKEN_ENV)
    if token_str:
        credentials = auth.credentials_from_token_string(token_str)
    elif args.token_file:
        if not auth.check_token_permissions(args.token_file):
            print(f"warning: token file {args.token_file} is not owner-only (0600)", file=sys.stderr)
        credentials = auth.load_token_file(args.token_file)
    else:
        print(f"set {auth.TOKEN_ENV} or pass --token-file", file=sys.stderr)
        return 2

    service = build("youtube", "v3", credentials=credentials)
    uploader = Uploader(service, playlist_id=args.playlist)
    video_id = uploader.upload(
        args.video, args.title, args.description, privacy=args.privacy
    )
    print(f"uploaded video id: {video_id}")
    print(f"quota used: {uploader.quota.used}/{uploader.quota.daily_limit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
