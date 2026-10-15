"""Reconcile the local plan against what is already on YouTube.

Every uploaded segment carries its ``segment_id`` in the video description as
a marker line. Listing the channel's uploads then matches local segments to
remote videos without relying solely on the local database, so a lost or
absent ``tubetape.json`` does not cause the whole library to be re-uploaded.
"""

from __future__ import annotations

import re
from .log import get_logger

_logger = get_logger("reconcile")

SEGMENT_ID_MARKER = "tubetape-segment-id:"
TITLE_SEGMENT_ID_RE = re.compile(r"\[([0-9a-fA-F]{12,64})\]|[-_]([0-9a-fA-F]{12,64})$")


def extract_segment_id_from_title(title: str) -> str | None:
    """Extract a segment id from a video title (e.g. ``... [a1b2c3d4e5f67890]``)."""
    if not title:
        return None
    match = TITLE_SEGMENT_ID_RE.search(title.strip())
    if match:
        return match.group(1) or match.group(2)
    return None


def embed_segment_id(description: str, segment_id: str) -> str:
    """Append the segment-id marker to a video description (idempotent)."""
    if SEGMENT_ID_MARKER in description:
        return description
    description = description.rstrip()
    separator = "\n\n" if description else ""
    return f"{description}{separator}{SEGMENT_ID_MARKER} {segment_id}"


def extract_segment_id(description: str) -> str | None:
    """Return the segment id embedded in a description, or None."""
    if not description:
        return None
    for line in description.splitlines():
        line = line.strip()
        if line.startswith(SEGMENT_ID_MARKER):
            value = line[len(SEGMENT_ID_MARKER):].strip()
            if value:
                return value
    return None


def get_uploads_playlist_id(service) -> str | None:
    """Return the authenticated channel's uploads playlist id."""
    response = service.channels().list(part="contentDetails", mine=True).execute()
    items = response.get("items") or []
    if not items:
        return None
    return (
        items[0].get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads")
    )


class RemoteIndex(dict):
    """A dictionary mapping segment_id -> youtube_video_id with channel catalog metadata."""

    def __init__(self, mapping=None, all_video_ids=None, success=True, **kwargs):
        super().__init__(mapping or {}, **kwargs)
        self.all_video_ids: set[str] = set(all_video_ids or ())
        self.success: bool = success


def fetch_remote_index(service) -> RemoteIndex:
    """Return ``{segment_id: video_id}`` for every tubetape video on the channel.

    Matches segment IDs embedded in video titles (preferred, never truncated by
    YouTube) as well as descriptions (legacy fallback).
    """
    try:
        uploads = get_uploads_playlist_id(service)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("cannot list channel uploads: %s", exc)
        return RemoteIndex(success=False)
    if not uploads:
        _logger.warning("no uploads playlist found for this channel")
        return RemoteIndex(success=False)

    index: dict[str, str] = {}
    all_video_ids: set[str] = set()
    page_token = None
    pages = 0
    try:
        while True:
            response = (
                service.playlistItems()
                .list(
                    part="snippet",
                    playlistId=uploads,
                    maxResults=50,
                    pageToken=page_token,
                )
                .execute()
            )
            pages += 1
            for item in response.get("items") or []:
                snippet = item.get("snippet") or {}
                video_id = (snippet.get("resourceId") or {}).get("videoId")
                if video_id:
                    all_video_ids.add(video_id)
                title = snippet.get("title") or ""
                desc = snippet.get("description") or ""
                sid = extract_segment_id_from_title(title) or extract_segment_id(desc)
                if video_id and sid:
                    index[sid] = video_id
                    if len(sid) >= 16:
                        index[sid[:16]] = video_id
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:  # noqa: BLE001
        _logger.warning("error while listing uploads: %s", exc)
        return RemoteIndex(index, all_video_ids=all_video_ids, success=False)

    _logger.info(
        "remote index: %d tubetape segment(s), %d total video(s) from %d upload page(s)",
        len(index),
        len(all_video_ids),
        pages,
    )
    return RemoteIndex(index, all_video_ids=all_video_ids, success=True)


def fetch_remote_catalog(service) -> tuple[dict[str, str], set[str], bool]:
    """Return ({segment_id: video_id}, {all_video_ids}, success) for every video on the channel."""
    res = fetch_remote_index(service)
    if isinstance(res, RemoteIndex):
        return res, res.all_video_ids, res.success
    return res, getattr(res, "all_video_ids", set()), getattr(res, "success", True)


def reconcile_database(
    db,
    remote_index: dict[str, str],
    remote_video_ids: set[str] | None = None,
    planned_segments: list | None = None,
) -> dict[str, int]:
    """Perform two-way reconciliation between local database and YouTube catalog.

    Returns dict with summary counts:
      - 'matched': count of segments updated with remote video_id
      - 'deleted': count of segments whose YouTube video was detected deleted on remote
      - 'unchanged': count of segments already in sync
    """
    import os
    from .db import SEGMENT_STATUS_SEALED

    matched = 0
    deleted = 0
    unchanged = 0

    if not db or not hasattr(db, "segments"):
        return {"matched": 0, "deleted": 0, "unchanged": 0}

    # 1. Reconcile existing records in db.segments
    for sid, srec in list(db.segments.items()):
        cur_vid = srec.get("youtube_video_id")

        if cur_vid:
            # Direction 2: Local recorded a video_id, check if still exists on YouTube
            if remote_video_ids is not None and cur_vid not in remote_video_ids:
                prev = list(srec.get("previous_video_ids", []))
                if cur_vid not in prev:
                    prev.append(cur_vid)
                srec["previous_video_ids"] = prev
                srec["youtube_video_id"] = None
                out = srec.get("output_path")
                if not (out and os.path.isfile(out)):
                    srec["status"] = "pending"
                _logger.info("reconcile: segment %s video %s no longer on YouTube; reset", sid, cur_vid)
                deleted += 1
            else:
                unchanged += 1
        else:
            # Direction 1: Local missing video_id, check if found on YouTube
            remote_vid = remote_index.get(sid) or (remote_index.get(sid[:16]) if len(sid) >= 16 else None)
            if remote_vid:
                srec["youtube_video_id"] = remote_vid
                srec["status"] = SEGMENT_STATUS_SEALED
                srec["error"] = None
                _logger.info("reconcile: segment %s matched existing YouTube video %s", sid, remote_vid)
                matched += 1
            else:
                unchanged += 1

    # 2. Check planned segments not yet in db.segments
    if planned_segments:
        for seg in planned_segments:
            sid = getattr(seg, "segment_id", None) or (seg.get("segment_id") if isinstance(seg, dict) else "")
            if not sid or sid in db.segments:
                continue
            remote_vid = remote_index.get(sid) or (remote_index.get(sid[:16]) if len(sid) >= 16 else None)
            if remote_vid:
                title = getattr(seg, "title", None) or (seg.get("title") if isinstance(seg, dict) else sid[:16])
                fids = list(getattr(seg, "file_ids", []) if hasattr(seg, "file_ids") else (seg.get("file_ids", []) if isinstance(seg, dict) else []))
                start_ts = getattr(seg, "start_ts", "") if hasattr(seg, "start_ts") else (seg.get("start_ts", "") if isinstance(seg, dict) else "")
                end_ts = getattr(seg, "end_ts", "") if hasattr(seg, "end_ts") else (seg.get("end_ts", "") if isinstance(seg, dict) else "")
                dur = float(getattr(seg, "duration_seconds", 0.0) if hasattr(seg, "duration_seconds") else (seg.get("duration_seconds", 0.0) if isinstance(seg, dict) else 0.0))
                db.upsert_segment(
                    sid,
                    {
                        "title": title,
                        "file_ids": fids,
                        "range": [start_ts, end_ts],
                        "duration_seconds": dur,
                        "output_path": None,
                        "youtube_video_id": remote_vid,
                        "previous_video_ids": [],
                        "status": SEGMENT_STATUS_SEALED,
                        "chapters": [],
                        "last_rebuilt_at": None,
                        "attempts": 0,
                        "error": None,
                    },
                )
                _logger.info("reconcile: planned segment %s matched existing YouTube video %s", sid, remote_vid)
                matched += 1

    if matched > 0 or deleted > 0:
        db.save()

    return {"matched": matched, "deleted": deleted, "unchanged": unchanged}

