"""Reconcile the local plan against what is already on YouTube.

Every uploaded segment carries its ``segment_id`` in the video description as
a marker line. Listing the channel's uploads then matches local segments to
remote videos without relying solely on the local database, so a lost or
absent ``tubetape.json`` does not cause the whole library to be re-uploaded.
"""

from __future__ import annotations

from .log import get_logger

_logger = get_logger("reconcile")

SEGMENT_ID_MARKER = "tubetape-segment-id:"


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


def fetch_remote_index(service) -> dict[str, str]:
    """Return ``{segment_id: video_id}`` for every tubetape video on the channel.

    Videos without the marker are ignored. Any error is logged and yields an
    empty/partial index: reconciliation is best-effort and must never block
    uploads.
    """
    try:
        uploads = get_uploads_playlist_id(service)
    except Exception as exc:  # noqa: BLE001
        _logger.warning("cannot list channel uploads: %s", exc)
        return {}
    if not uploads:
        _logger.warning("no uploads playlist found for this channel")
        return {}

    index: dict[str, str] = {}
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
                segment_id = extract_segment_id(snippet.get("description") or "")
                if video_id and segment_id:
                    index[segment_id] = video_id
            page_token = response.get("nextPageToken")
            if not page_token:
                break
    except Exception as exc:  # noqa: BLE001
        _logger.warning("error while listing uploads: %s", exc)
        return index

    _logger.info(
        "remote index: %d tubetape segment(s) from %d upload page(s)",
        len(index),
        pages,
    )
    return index
