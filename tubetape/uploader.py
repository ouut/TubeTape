"""YouTube upload with retry and quota-error handling.

Uploads are resumable and retried with exponential backoff. A quota-exceeded
error is not retried: it is surfaced as :class:`QuotaExceededError` so the
caller can stop and back off until the quota resets.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Callable

from googleapiclient.http import MediaFileUpload

from .log import get_logger

_logger = get_logger("uploader")

DEFAULT_CATEGORY_ID = "22"  # People & Blogs


class QuotaExceededError(RuntimeError):
    """Raised when the YouTube upload quota is exhausted."""


def build_upload_body(
    title: str,
    description: str,
    privacy: str = "private",
    category_id: str = DEFAULT_CATEGORY_ID,
    made_for_kids: bool = False,
) -> dict:
    """Build the ``videos.insert`` body with all required metadata fields."""
    return {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": made_for_kids,
            "selfCertification": {"selfDeclaredMadeForKids": made_for_kids},
        },
    }


def is_quota_error(exc: Exception) -> bool:
    """Detect a YouTube quota-exceeded error from an HttpError."""
    status = getattr(exc, "status", None)
    content = getattr(exc, "content", None)
    if status == 403 and content:
        try:
            data = json.loads(content.decode("utf-8", errors="replace"))
            for item in data.get("error", {}).get("errors", []):
                if "quota" in str(item.get("reason", "")).lower():
                    return True
        except (ValueError, AttributeError):
            pass
    return "quota" in str(exc).lower()


DEFAULT_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB resumable chunks


class Uploader:
    def __init__(
        self,
        service,
        playlist_id: str | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ):
        self.service = service
        self.playlist_id = playlist_id
        self.chunk_size = chunk_size

    def upload(
        self,
        media_path: str,
        title: str,
        description: str,
        privacy: str = "private",
        category_id: str = DEFAULT_CATEGORY_ID,
        progress_callback=None,
        max_retries: int = 5,
        base_delay: float = 2.0,
        sleep=time.sleep,
    ) -> str:
        """Upload a media file and return the new video ID."""
        size = os.path.getsize(media_path)
        _logger.info(
            "uploading %s (%.1f MB) as %r (privacy=%s, category=%s)",
            media_path,
            size / (1024 * 1024),
            title,
            privacy,
            category_id,
        )
        body = build_upload_body(title, description, privacy, category_id, made_for_kids=False)
        media = MediaFileUpload(media_path, chunksize=self.chunk_size, resumable=True)
        request = self.service.videos().insert(part="snippet,status", body=body, media_body=media)

        response = self._execute_resumable_upload(
            request,
            max_retries=max_retries,
            base_delay=base_delay,
            sleep=sleep,
            progress_callback=progress_callback,
        )

        video_id = response.get("id")
        _logger.info("upload complete: video id %s", video_id)
        if self.playlist_id:
            self.add_to_playlist(video_id)
        return video_id

    def _execute_resumable_upload(
        self,
        request,
        max_retries: int = 5,
        base_delay: float = 2.0,
        sleep=time.sleep,
        progress_callback=None,
    ):
        if hasattr(request, "next_chunk"):
            response = None
            retry = 0
            last_log = time.monotonic()
            while response is None:
                try:
                    status, response = request.next_chunk()
                    if status:
                        prog_val = status.progress()
                        uploaded_bytes = getattr(status, "resumable_progress", 0)
                        total_bytes = getattr(status, "total_size", 0)
                        if progress_callback:
                            try:
                                progress_callback(prog_val, uploaded_bytes, total_bytes)
                            except TypeError:
                                progress_callback(prog_val)
                        now = time.monotonic()
                        if now - last_log >= 15.0:
                            last_log = now
                            _logger.info("[上传中] upload progress: %.1f%%", prog_val * 100.0)
                    retry = 0
                except Exception as exc:
                    if is_quota_error(exc):
                        _logger.warning("upload hit quota error: %s", exc)
                        raise QuotaExceededError(f"upload quota exceeded: {exc}") from exc
                    retry += 1
                    if retry > max_retries:
                        _logger.error("resumable upload failed after %d attempt(s): %s", retry, exc)
                        raise
                    delay = base_delay * (2 ** (retry - 1))
                    _logger.warning(
                        "upload chunk failed (%s); retrying in %.1fs (attempt %d/%d)",
                        exc,
                        delay,
                        retry,
                        max_retries,
                    )
                    sleep(delay)
            return response
        return self._execute_with_retry(request, max_retries=max_retries, base_delay=base_delay, sleep=sleep)

    def _execute_with_retry(self, request, max_retries=5, base_delay=2.0, sleep=time.sleep):
        for attempt in range(max_retries + 1):
            try:
                return request.execute()
            except Exception as exc:  # noqa: BLE001 - retry transient errors
                if is_quota_error(exc):
                    _logger.warning("upload hit quota error: %s", exc)
                    raise QuotaExceededError(f"upload quota exceeded: {exc}") from exc
                if attempt == max_retries:
                    _logger.error("upload failed after %d attempt(s): %s", attempt + 1, exc)
                    raise
                delay = base_delay * (2 ** attempt)
                _logger.warning(
                    "upload attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                    delay,
                )
                sleep(delay)
        raise RuntimeError("retries exhausted")

    def add_to_playlist(self, video_id: str, playlist_id: str | None = None) -> dict:
        """Append a video to a playlist (segments are uploaded in time order)."""
        pid = playlist_id or self.playlist_id
        if not pid:
            return {}
        _logger.info("adding video %s to playlist %s", video_id, pid)
        try:
            request = self.service.playlistItems().insert(
                part="snippet",
                body={
                    "snippet": {
                        "playlistId": pid,
                        "resourceId": {"kind": "youtube#video", "videoId": video_id},
                    }
                },
            )
            return request.execute()
        except Exception as exc:  # noqa: BLE001 - playlist errors should not abort upload success
            _logger.warning("could not add video %s to playlist %s: %s (continuing)", video_id, pid, exc)
            return {}

    def verify(self, video_id: str) -> None:
        """Verify an uploaded video exists; raise if it does not."""
        _logger.info("verifying uploaded video %s exists", video_id)
        response = self.service.videos().list(part="status", id=video_id).execute()
        if not response.get("items"):
            _logger.error("uploaded video %s not found during verification", video_id)
            raise RuntimeError(f"uploaded video {video_id} not found")
        _logger.debug("verified video %s", video_id)

    def delete_video(self, video_id: str) -> None:
        """Delete a video (used by rebuild). Requires the youtube.force-ssl scope."""
        _logger.info("deleting old video %s", video_id)
        self.service.videos().delete(id=video_id).execute()
        _logger.debug("deleted video %s", video_id)


class UploadTask:
    """A task representing an uploaded segment video."""

    def __init__(
        self,
        segment_id: str,
        title: str,
        out_path: str,
        description: str,
        privacy: str = "private",
        on_complete: Callable[[str], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ):
        self.segment_id = segment_id
        self.title = title
        self.out_path = out_path
        self.description = description
        self.privacy = privacy
        self.on_complete = on_complete
        self.on_error = on_error


class UploadWorker:
    """Background single-threaded worker for sequential YouTube uploads.

    Decouples video transcoding from network uploading:
    - Transcoder finishes a segment and pushes it into UploadWorker.
    - UploadWorker processes uploads one by one in the background.
    - Transcoder immediately starts on the next segment without waiting for network I/O.
    """

    def __init__(
        self,
        uploader: Uploader,
        reporter=None,
    ):
        self.uploader = uploader
        self.reporter = reporter
        self._queue: queue.Queue[UploadTask | None] = queue.Queue()
        self._queued_ids: set[str] = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self.active_task: UploadTask | None = None
        self.quota_error: QuotaExceededError | None = None
        self._thread = threading.Thread(target=self._run, name="TubeTape-UploadWorker", daemon=True)
        self._thread.start()

    @property
    def queued_ids(self) -> set[str]:
        with self._lock:
            return set(self._queued_ids)

    def enqueue(self, task: UploadTask) -> None:
        """Enqueue an upload task."""
        with self._lock:
            self._queued_ids.add(task.segment_id)
        self._queue.put(task)
        try:
            from . import web
            web.set_upload_queue(list(self.queued_ids))
        except Exception:
            pass
        _logger.info("enqueued segment %s (%s) for background upload", task.segment_id, task.title)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if task is None:
                self._queue.task_done()
                break

            with self._lock:
                self.active_task = task
            sid = task.segment_id
            title = task.title
            out_path = task.out_path

            try:
                if self.quota_error is not None:
                    _logger.warning("skipping upload of %s due to quota exhaustion", title)
                    if task.on_error:
                        task.on_error(self.quota_error)
                    continue

                if not os.path.isfile(out_path):
                    err = FileNotFoundError(f"segment file {out_path} not found")
                    _logger.error("cannot upload %s: %s", title, err)
                    if task.on_error:
                        task.on_error(err)
                    continue

                file_size = os.path.getsize(out_path)
                try:
                    from . import web
                    web.update_web_upload(sid, title=title, percent=0.0, uploaded_bytes=0, total_bytes=file_size)
                    web.set_web_status("uploading", f"正在上传 {title} (0.0%)")
                except Exception:
                    pass

                if self.reporter:
                    self.reporter.status(f"uploading {title} (size: {file_size / (1024 * 1024):.1f} MB) ...")

                start_time = time.monotonic()
                last_rep_pct = -5.0
                last_rep_time = start_time

                def _progress_cb(pct: float, uploaded_b: int = 0, total_b: int = 0) -> None:
                    pct_val = pct * 100.0
                    now = time.monotonic()
                    elapsed = max(0.001, now - start_time)
                    speed_bps = uploaded_b / elapsed if uploaded_b > 0 else 0
                    speed_str = f"{speed_bps / (1024 * 1024):.1f} MB/s" if speed_bps > 0 else "N/A"

                    try:
                        from . import web
                        web.update_web_upload(
                            sid,
                            title=title,
                            percent=pct_val,
                            uploaded_bytes=uploaded_b,
                            total_bytes=total_b or file_size,
                            speed=speed_str,
                        )
                        web.set_web_status("uploading", f"正在上传 {title} ({pct_val:.1f}%) · {speed_str}")
                    except Exception:
                        pass

                    nonlocal last_rep_pct, last_rep_time
                    if (pct_val - last_rep_pct >= 5.0) or (now - last_rep_time >= 10.0) or pct_val >= 99.9:
                        last_rep_pct = pct_val
                        last_rep_time = now
                        if self.reporter and hasattr(self.reporter, "upload_progress"):
                            self.reporter.upload_progress(title, pct_val, uploaded_b, total_b or file_size, speed_str)

                video_id = self.uploader.upload(
                    out_path,
                    title,
                    task.description,
                    privacy=task.privacy,
                    progress_callback=_progress_cb,
                )
                _logger.info("upload complete for %s -> %s", sid, video_id)
                try:
                    from . import web
                    web.clear_web_upload()
                except Exception:
                    pass
                if self.reporter:
                    self.reporter.status(f"upload complete: {title} -> https://youtu.be/{video_id}")
                if task.on_complete:
                    task.on_complete(video_id)

            except QuotaExceededError as q_exc:
                self.quota_error = q_exc
                _logger.warning("YouTube quota exceeded during upload of %s: %s", title, q_exc)
                try:
                    from . import web
                    web.clear_web_upload()
                    web.set_web_status("warning", "YouTube 上传配额已耗尽，已暂停后台上传")
                except Exception:
                    pass
                if self.reporter:
                    self.reporter.warning("YouTube upload quota exhausted for today! Uploading paused.")
                if task.on_error:
                    task.on_error(q_exc)
            except Exception as exc:
                _logger.error("upload error on segment %s: %s", sid, exc, exc_info=True)
                try:
                    from . import web
                    web.clear_web_upload()
                except Exception:
                    pass
                if self.reporter:
                    self.reporter.error(f"upload failed for {title}: {exc}")
                if task.on_error:
                    task.on_error(exc)
            finally:
                with self._lock:
                    self._queued_ids.discard(sid)
                    self.active_task = None
                try:
                    from . import web
                    web.set_upload_queue(list(self.queued_ids))
                except Exception:
                    pass
                self._queue.task_done()

    def wait_done(self) -> None:
        """Wait until all currently queued uploads are finished."""
        self._queue.join()

    def stop(self) -> None:
        """Stop worker thread."""
        self._stop_event.set()
        self._queue.put(None)
        self._thread.join(timeout=5.0)

