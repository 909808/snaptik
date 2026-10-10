"""Application logic: URL validation, rate limits, caching and the backend, with no HTTP in it."""
from __future__ import annotations

import logging
from math import ceil
from typing import Any
from urllib.parse import urlencode

import httpx

from .backend import Backend, Download
from .config import Settings
from .errors import Busy, FormatUnavailable, InvalidRequest, RateLimited
from .limits import Gate, SlidingWindowLimiter, TTLCache
from .media import DOWNLOAD_KINDS, build_options, is_slideshow
from .urls import resolve_target

log = logging.getLogger(__name__)

SLIDESHOW_NOTE = "This post is a photo slideshow, so only its sound and cover can be downloaded."


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
        self._limiters = {
            "lookup": SlidingWindowLimiter(settings.rate_limit_lookup),
            "download": SlidingWindowLimiter(settings.rate_limit_download),
        }

    def close(self) -> None:
        self.http.close()

    def check_rate(self, bucket: str, client: str) -> None:
        wait = self._limiters[bucket].check(client)
        if wait is not None:
            raise RateLimited(retry_after=max(1, ceil(wait)))

    # ------------------------------------------------------------------ lookup

    def lookup(self, text: str) -> dict[str, Any]:
        """Resolve pasted text to a post and describe what can be downloaded from it."""
        target = resolve_target(text, self.http)
        assert target.video_id is not None
        if (cached := self._cache.get(target.video_id)) is not None:
            return cached

        if not self._lookup_gate.acquire(timeout=10):
            raise Busy(retry_after=5)
        try:
            info = self.backend.fetch_info(target.canonical_url)
        finally:
            self._lookup_gate.release()

        options = build_options(info, ffmpeg_available=self.backend.ffmpeg_available)
        if not options:
            raise FormatUnavailable("TikTok didn't provide anything downloadable for this post.")
        payload: dict[str, Any] = {
            "id": info.id,
            "url": info.url,
            "title": info.title or info.description or "TikTok video",
            "description": info.description,
            "author": {"name": info.author_name, "handle": info.author_handle},
            "thumbnail": info.thumbnail,
            "duration": info.duration,
            "stats": info.stats,
            "note": SLIDESHOW_NOTE if is_slideshow(info) else None,
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

    # --------------------------------------------------------------- download

    def open_download(self, url: str, kind: str) -> Download:
        if kind not in DOWNLOAD_KINDS:
            raise InvalidRequest("Unknown download type.")
        target = resolve_target(url, self.http)
        if not self._download_gate.acquire():
            raise Busy(retry_after=5)
        try:
            download = self.backend.open_download(target.canonical_url, kind)
        except BaseException:
            self._download_gate.release()
            raise
        download.add_cleanup(self._download_gate.release)  # frees the slot however the transfer ends
        return download
