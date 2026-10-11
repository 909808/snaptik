"""Application logic: URL validation, rate limits, caching, the backend and bulk jobs, with no HTTP in it."""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterator, Sequence
from math import ceil
from typing import Any
from urllib.parse import urlencode

import httpx

from .archive import Pacer, PostRef, Progress, Wants, archive_posts
from .backend import Backend, Download
from .config import Settings
from .errors import Busy, Disabled, FormatUnavailable, InvalidRequest, InvalidURL, RateLimited, UnknownJob
from .limits import Gate, SlidingWindowLimiter, TTLCache
from .media import DOWNLOAD_KINDS, ProfileListing, build_options, is_slideshow, iso_date, safe_filename
from .streaming import BlockingStream
from .urls import resolve_target
from .zipstream import ZipStream

log = logging.getLogger(__name__)

SLIDESHOW_NOTE = "This post is a photo slideshow, so only its sound and cover can be downloaded."
SLIDESHOW_PHOTOS_NOTE = "This post is a photo slideshow. Download all its photos as a ZIP, plus the sound."

_HANDLE = re.compile(r"[A-Za-z0-9_.\-]{1,100}")
_POST_ID = re.compile(r"\d{6,25}")
_JOB_ID = re.compile(r"[A-Za-z0-9]{16,32}")
_JOB_KEEP_SECONDS = 900  # how long a finished run's progress stays readable


class BulkRun:
    """One bulk download. It owns the concurrency slot and the progress record, and releases them
    exactly once however it ends: finished, stopped, client gone, or never started at all."""

    def __init__(self, service: Service, handle: str, refs: list[PostRef], wants: Wants, progress: Progress) -> None:
        self._service = service
        self._handle = handle
        self._refs = refs
        self._wants = wants
        self.progress = progress
        self.filename = safe_filename(f"tiktok_{handle}_{len(refs)}_posts", "zip")
        self._lock = threading.Lock()
        self._closed = False

    def stream(self) -> BlockingStream:
        return BlockingStream(self._produce, cancel=self.progress.cancel)

    def _produce(self, cancel: threading.Event) -> Iterator[bytes]:
        settings = self._service.settings
        # A short back-off: the browser's connection is open the whole time, so long waits belong to the CLI.
        pacer = Pacer(
            cancel.wait,
            delay=settings.bulk_delay,
            backoff_start=5.0,
            backoff_max=float(settings.bulk_max_backoff),
            max_blocks=2,
            max_retries=1,
        )
        archive = ZipStream()
        root = f"tiktok_{self._handle}"
        try:
            for file in archive_posts(
                self._service.backend,
                self._refs,
                self._wants,
                pacer=pacer,
                progress=self.progress,
                max_bytes=settings.max_download_mb * 1024 * 1024,
                temp_dir=settings.temp_dir,
                deadline=time.monotonic() + settings.bulk_max_seconds,
            ):
                try:
                    yield from archive.add_stream(f"{root}/{file.path}", file.chunks(), file.mtime)
                finally:
                    file.close()
            yield from archive.finish()
        finally:
            archive.close()
            self.close()

    def close(self) -> None:
        with self._lock:
            if getattr(self, "_closed", True):
                return
            self._closed = True
        self.progress.finish("stopped", "The download was interrupted.")
        self._service._bulk_gate.release()

    def __del__(self) -> None:
        self.close()


class Service:
    def __init__(self, settings: Settings, backend: Backend, *, http: httpx.Client | None = None) -> None:
        self.settings = settings
        self.backend = backend
        # TikTok's short links answer with a redirect only to crawler-looking clients.
        self.http = http or httpx.Client(
            timeout=httpx.Timeout(settings.network_timeout),
            follow_redirects=False,
            proxy=settings.proxy_url,
            headers={"User-Agent": "facebookexternalhit/1.1"},
        )
        self._cache: TTLCache[str, dict[str, Any]] = TTLCache(settings.cache_size, settings.cache_ttl)
        self._lookup_gate = Gate(settings.max_concurrent_lookups)
        self._download_gate = Gate(settings.max_concurrent_downloads)
        self._bulk_gate = Gate(settings.max_concurrent_bulk)
        self._limiters = {
            "lookup": SlidingWindowLimiter(settings.rate_limit_lookup),
            "download": SlidingWindowLimiter(settings.rate_limit_download),
            "bulk": SlidingWindowLimiter(settings.rate_limit_bulk, window=3600.0),
        }
        self._jobs: dict[str, Progress] = {}
        self._jobs_lock = threading.Lock()

    def close(self) -> None:
        self.http.close()

    def check_rate(self, bucket: str, client: str) -> None:
        wait = self._limiters[bucket].check(client)
        if wait is not None:
            raise RateLimited(retry_after=max(1, ceil(wait)))

    # ------------------------------------------------------------------ lookup

    def lookup(self, text: str) -> dict[str, Any]:
        """Resolve pasted text to a post or a profile and describe what can be downloaded from it."""
        target = resolve_target(text, self.http)
        if target.profile is not None:
            return self._lookup_profile(target.profile)
        assert target.video_id is not None
        if (cached := self._cache.get(target.video_id)) is not None:
            return cached

        info = self._guarded(lambda: self.backend.fetch_info(target.canonical_url))
        options = build_options(info, ffmpeg_available=self.backend.ffmpeg_available)
        if not options:
            raise FormatUnavailable("TikTok didn't provide anything downloadable for this post.")
        payload: dict[str, Any] = {
            "type": "post",
            "id": info.id,
            "url": info.url,
            "title": info.title or info.description or "TikTok video",
            "description": info.description,
            "author": {"name": info.author_name, "handle": info.author_handle},
            "thumbnail": info.thumbnail,
            "duration": info.duration,
            "stats": info.stats,
            "note": (SLIDESHOW_PHOTOS_NOTE if info.images else SLIDESHOW_NOTE) if is_slideshow(info) else None,
            "downloads": [
                {
                    "kind": o.kind,
                    "title": o.title,
                    "detail": o.detail,
                    "tag": o.tag,
                    "ext": o.ext,
                    "primary": o.primary,
                    "href": "/api/download?" + urlencode({"url": info.url, "kind": o.kind}),
                }
                for o in options
            ],
        }
        self._cache.set(target.video_id, payload)
        return payload

    def _guarded(self, work: Any) -> Any:
        """Run a TikTok lookup under the lookup concurrency limit."""
        if not self._lookup_gate.acquire(timeout=10):
            raise Busy(retry_after=5)
        try:
            return work()
        finally:
            self._lookup_gate.release()

    def _lookup_profile(self, handle: str) -> dict[str, Any]:
        key = f"@{handle.lower()}"
        if (cached := self._cache.get(key)) is not None:
            return cached
        listing: ProfileListing = self._guarded(lambda: self.backend.list_profile(handle, self.settings.profile_list_limit))
        payload = {
            "type": "profile",
            "handle": listing.handle,
            "name": listing.name,
            "url": f"https://www.tiktok.com/@{listing.handle}",
            "posts": [
                {
                    "id": p.id,
                    "url": p.url,
                    "caption": p.caption,
                    "date": iso_date(p.timestamp),
                    "duration": p.duration,
                    "thumbnail": p.thumbnail,
                    "views": p.stats.get("views"),
                    "likes": p.stats.get("likes"),
                }
                for p in listing.posts
            ],
            "more": listing.more,
            "warning": listing.warning,
            "bulk": {
                "enabled": self.settings.bulk_enabled,
                "max_posts": self.settings.bulk_max_posts,
                "delay": self.settings.bulk_delay,
                "audio": True,
            },
        }
        self._cache.set(key, payload)
        return payload

    # --------------------------------------------------------------- download

    def open_download(self, url: str, kind: str) -> Download:
        if kind not in DOWNLOAD_KINDS:
            raise InvalidRequest("Unknown download type.")
        target = resolve_target(url, self.http)
        if target.video_id is None:
            raise InvalidURL("That's a profile link. Paste it in the box to list its posts instead.")
        if not self._download_gate.acquire():
            raise Busy(retry_after=5)
        try:
            download = self.backend.open_download(target.canonical_url, kind)
        except BaseException:
            self._download_gate.release()
            raise
        download.add_cleanup(self._download_gate.release)  # frees the slot however the transfer ends
        return download

    # ------------------------------------------------------------------- bulk

    def start_bulk(self, handle: str, ids: Sequence[str], what: str, job_id: str) -> BulkRun:
        """Validate a request for many posts and reserve the (single) bulk slot for it."""
        if not self.settings.bulk_enabled:
            raise Disabled("Saving many posts at once is turned off on this server.")
        if not _HANDLE.fullmatch(handle):
            raise InvalidURL()
        if not _JOB_ID.fullmatch(job_id):
            raise InvalidRequest("Missing or malformed download id.")
        unique = list(dict.fromkeys(i.strip() for i in ids if i.strip()))
        if not unique or len(unique) > self.settings.bulk_max_posts or not all(_POST_ID.fullmatch(i) for i in unique):
            raise InvalidRequest(f"Pick between 1 and {self.settings.bulk_max_posts} posts.")
        wants = Wants.parse(what)
        refs = [PostRef(i, f"https://www.tiktok.com/@{handle}/video/{i}") for i in unique]

        if not self._bulk_gate.acquire():
            raise Busy("Another bulk download is running on this server. Try again in a few minutes.", retry_after=30)
        progress = Progress(len(refs))
        try:
            self._track(job_id, progress)
        except BaseException:
            self._bulk_gate.release()
            raise
        return BulkRun(self, handle, refs, wants, progress)

    def _track(self, job_id: str, progress: Progress) -> None:
        with self._jobs_lock:
            now = time.monotonic()
            for key, job in list(self._jobs.items()):
                if job.finished_at is not None and now - job.finished_at > _JOB_KEEP_SECONDS:
                    del self._jobs[key]
            if (existing := self._jobs.get(job_id)) is not None and existing.finished_at is None:
                raise InvalidRequest("That download is already running.")
            self._jobs[job_id] = progress

    def _job(self, job_id: str) -> Progress:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise UnknownJob()
        return job

    def bulk_progress(self, job_id: str) -> dict[str, Any]:
        return self._job(job_id).snapshot()

    def stop_bulk(self, job_id: str) -> dict[str, Any]:
        """Ask a run to stop after the post it is on; the ZIP it has built so far is still completed."""
        job = self._job(job_id)
        job.cancel.set()
        return job.snapshot()
