"""yt-dlp integration: look a post (or a profile) up, then stream the chosen files to the client.

The rest of the app only knows the ``Backend`` protocol, so tests can swap in a
fake and this file stays the single place that talks to TikTok.
"""
from __future__ import annotations

import io
import ipaddress
import json
import logging
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.extractor.tiktok import TikTokIE, TikTokUserIE
from yt_dlp.networking import Request
from yt_dlp.networking.impersonate import ImpersonateTarget
from yt_dlp.utils import DownloadError, ExtractorError, YoutubeDLError

from .config import Settings
from .errors import (
    AppError,
    ConversionFailed,
    FormatUnavailable,
    LoginRequired,
    TooLarge,
    UpstreamBlocked,
    UpstreamError,
    VideoUnavailable,
)
from .media import (
    KIND_AUDIO,
    KIND_COVER,
    KIND_IMAGES,
    KIND_META,
    KIND_VIDEO_SD,
    KIND_VIDEO_WM,
    VIDEO_KINDS,
    MediaFormat,
    ProfileListing,
    ProfilePost,
    VideoInfo,
    audio_plan,
    download_stem,
    parse_info,
    parse_profile_entry,
    post_record,
    safe_filename,
    video_candidates,
)
from .zipstream import IterReader, ZipStream

log = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024
MAX_FORMAT_ATTEMPTS = 4  # CDN URLs that fail to open before we give up
MAX_PHOTO_BYTES = 50 * 1024 * 1024
LISTING_RETRIES = 5  # yt-dlp's own tests use up to 10: TikTok sometimes repeats a page of a profile
LISTING_PAUSE = 0.4  # seconds between the pages of a long listing (a whole profile is hundreds of requests)
LISTING_PAUSE_ABOVE = 100  # ... but only past this many posts, so the page's own short listing stays quick
TEMP_PREFIX = "snaptik-"
TIKTOK_REFERER = "https://www.tiktok.com/"

_AUDIO_TYPES = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac", "ogg": "audio/ogg", "opus": "audio/ogg"}
_VIDEO_SUFFIX = {KIND_VIDEO_SD: "sd", KIND_VIDEO_WM: "watermarked"}  # so saving two renditions never collides
# Raster formats only: never relay something like SVG that could carry script under our origin.
_IMAGE_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/avif": "avif", "image/gif": "gif"}
# Content types that say nothing about the bytes: for these the file itself decides what it is.
_GENERIC_TYPES = {"", "application/octet-stream", "binary/octet-stream", "application/x-octet-stream"}


class Download:
    """A ready-to-send file: response metadata plus a chunked reader.

    ``close()`` is idempotent and also runs on garbage collection, so the upstream
    connection, temp files and concurrency slot are released even if the client
    disconnects before the first byte is sent. Cleanups run in the order they were
    registered: register inner resources first, so each owner outlives what it owns
    (response, then the yt-dlp session, then the concurrency slot).
    """

    def __init__(
        self,
        *,
        filename: str,
        media_type: str,
        length: int | None,
        reader: Callable[[int], bytes],
        cleanups: list[Callable[[], None]] | None = None,
    ) -> None:
        self.filename = filename
        self.media_type = media_type
        self.length = length
        self._reader = reader
        self._cleanups = list(cleanups or [])
        self._closed = False

    def read(self, size: int = CHUNK_SIZE) -> bytes:
        return self._reader(size)

    def add_cleanup(self, fn: Callable[[], None]) -> None:
        self._cleanups.append(fn)

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        while self._cleanups:
            try:
                self._cleanups.pop(0)()
            except Exception:  # noqa: BLE001 - cleanup must never raise
                log.debug("cleanup failed", exc_info=True)

    def __del__(self) -> None:
        self.close()


class Post(Protocol):
    """One looked-up post, from which each of its files can be opened without another lookup."""

    info: VideoInfo

    def open(self, kind: str) -> Download: ...

    def open_image(self, index: int) -> Download: ...

    def close(self) -> None: ...

    def __enter__(self) -> Post: ...

    def __exit__(self, *exc: object) -> None: ...


class Backend(Protocol):
    ffmpeg_available: bool

    def fetch_info(self, url: str) -> VideoInfo: ...

    def open_download(self, url: str, kind: str) -> Download: ...

    def open_post(self, url: str) -> Post: ...

    def list_profile(self, handle: str, limit: int) -> ProfileListing: ...


# Phrases in yt-dlp / TikTok error messages, checked in this order (see yt_dlp/extractor/tiktok.py).
_BLOCKED = (
    "ip address is blocked", "challenge", "captcha", "http error 403", "http error 429", "too many requests",
    "failed to parse json",  # a page of the posts API that is TikTok's "access denied" HTML, not JSON
)  # fmt: skip
_LOGIN = ("requiring login", "log in", "login", "permission to view", "comfortable for some audiences")
_EMPTY = ("does not have any videos", "any videos posted")
_GONE = ("video not available", "private", "removed", "no longer available", "not exist", "http error 404")


def map_ytdlp_error(exc: BaseException) -> AppError:
    """Translate a yt-dlp failure into an error with a message fit for end users."""
    inner = exc.exc_info[1] if isinstance(exc, DownloadError) and exc.exc_info and exc.exc_info[1] else exc
    message = " ".join(str(inner).split())
    log.warning("yt-dlp could not read the post: %s", message[:500])
    text = message.lower()

    if any(phrase in text for phrase in _BLOCKED):
        return UpstreamBlocked()
    if any(phrase in text for phrase in _LOGIN):
        return LoginRequired()
    if any(phrase in text for phrase in _EMPTY):
        return VideoUnavailable("This account hasn't posted any public videos.")
    if any(phrase in text for phrase in _GONE):
        return VideoUnavailable()
    return UpstreamError()  # includes plain network failures, and "Service Unavailable" is deliberately not "gone"


def _listing_error(exc: BaseException) -> AppError:
    """``map_ytdlp_error`` with wording that fits listing a profile rather than reading one post."""
    mapped = map_ytdlp_error(exc)
    if isinstance(mapped, LoginRequired):
        return LoginRequired("This account is private, so its posts can't be listed.")
    if isinstance(mapped, UpstreamBlocked):
        return UpstreamBlocked(
            "TikTok refused to list this profile's posts to this connection. It may be blocking it right now: "
            "try again later, or from another network."
        )
    if "unable to extract secondary user id" in str(exc).lower():
        # yt-dlp's wording when the profile page held no account data: a misspelt name, or TikTok
        # showing this server a block page. Either way "try again in a moment" would mislead.
        return UpstreamError(
            "Couldn't read that profile. Check the spelling of the name; if it is right, "
            "TikTok may be refusing this server right now."
        )
    return mapped


class _YdlLogger:
    """Routes yt-dlp's output into ``logging`` instead of the console."""

    def debug(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp error: %s", msg)  # the same text arrives as an exception we map


def sweep_stale_tempdirs(root: str | None = None, max_age: float = 3600.0) -> None:
    """Remove conversion directories left behind by a crashed process."""
    base = Path(root or tempfile.gettempdir())
    cutoff = time.time() - max_age
    try:
        for path in base.glob(f"{TEMP_PREFIX}*"):
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
    except OSError:
        log.debug("temp sweep failed", exc_info=True)


def _content_length(response: Any) -> int | None:
    raw = response.headers.get("Content-Length")
    return int(raw) if raw and raw.isdigit() else None


def _tag(value: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()[:120]


def read_fully(download: Download, limit: int) -> bytes:
    """Read a whole (small) download into memory, refusing truncated or oversized transfers."""
    chunks: list[bytes] = []
    total = 0
    try:
        while chunk := download.read(CHUNK_SIZE):
            total += len(chunk)
            if total > limit:
                raise TooLarge()
            chunks.append(chunk)
    finally:
        download.close()
    if download.length is not None and total != download.length:
        raise UpstreamError("TikTok cut the transfer short. Please try again.")
    return b"".join(chunks)


def sniff_image(head: bytes) -> str | None:
    """The raster image type that a file's first bytes announce, or ``None``. SVG, HTML and the like never match."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis"):
        return "image/avif"
    return None


class _Prefixed:
    """A response whose first bytes were already read (to see what they are), handed back before the rest."""

    def __init__(self, response: Any, head: bytes) -> None:
        self._response = response
        self._head = head
        self.headers = response.headers

    def read(self, size: int = CHUNK_SIZE) -> bytes:
        if self._head:
            head, self._head = self._head, b""
            return head
        return self._response.read(size)

    def close(self) -> None:
        self._response.close()


def _read_head(response: Any, size: int = 16) -> bytes:
    head = b""
    while len(head) < size and (chunk := response.read(size - len(head))):
        head += chunk
    return head


def _extract_images(aweme: Any) -> list[list[str]]:
    """The photos of a slideshow post, read from TikTok's own post data (yt-dlp skips them).

    Web data has ``imagePost.images[].imageURL.urlList``; the mobile API spells it
    ``image_post_info.images[].display_image.url_list``. Both are accepted.
    """
    if not isinstance(aweme, Mapping):
        return []
    post = aweme.get("imagePost") or aweme.get("image_post_info")
    images = post.get("images") if isinstance(post, Mapping) else None
    result: list[list[str]] = []
    for image in images if isinstance(images, list) else []:
        if not isinstance(image, Mapping):
            continue
        urls: list[str] = []
        for holder_key in ("imageURL", "displayImage", "display_image"):
            holder = image.get(holder_key)
            if not isinstance(holder, Mapping):
                continue
            for list_key in ("urlList", "url_list"):
                candidates = holder.get(list_key)
                if isinstance(candidates, list):
                    urls += [u for u in candidates if isinstance(u, str) and u.lower().startswith(("http://", "https://"))]
        if urls:
            result.append(list(dict.fromkeys(urls)))
    return result


class _KeepsPhotos:
    """Mixin for yt-dlp's TikTok extractors: keep the photos of a slideshow post, which they do not extract.

    Page fetching, the anti-bot handling and all the parsing stay yt-dlp's; we only keep a
    piece of the post data that it discards. Photos travel in the info dict as ``snaptik_images``.
    """

    def _parse_aweme_video_web(self, aweme_detail: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        info = super()._parse_aweme_video_web(aweme_detail, *args, **kwargs)  # type: ignore[misc]
        if images := _extract_images(aweme_detail):
            info["snaptik_images"] = images
        return info


class TikTokPhotosIE(_KeepsPhotos, TikTokIE):
    """One post, with its photos if it is a slideshow."""


class TikTokPhotosUserIE(_KeepsPhotos, TikTokUserIE):
    """A profile's listing, which can then tell photo posts from videos."""


class PostSession:
    """One yt-dlp lookup of a post. Its files are fetched in the same session (so the CDN gets the
    cookies and headers it expects) and without looking the post up again, which matters when a
    whole profile is saved: one TikTok page load per post, not one per file."""

    def __init__(self, backend: YtDlpBackend, ydl: yt_dlp.YoutubeDL, info: VideoInfo) -> None:
        self._backend = backend
        self._ydl = ydl
        self.info = info

    def open(self, kind: str) -> Download:
        backend, ydl, info = self._backend, self._ydl, self.info
        if kind == KIND_COVER:
            return backend._open_cover(ydl, info)
        if kind == KIND_AUDIO:
            return backend._open_audio(ydl, info)
        if kind in VIDEO_KINDS:
            return backend._open_video(ydl, info, kind)
        if kind == KIND_IMAGES:
            return backend._open_photos_zip(ydl, info)
        if kind == KIND_META:
            return backend._open_meta(info)
        raise FormatUnavailable()

    def open_image(self, index: int) -> Download:
        return self._backend._open_image(self._ydl, self.info, index)

    def close(self) -> None:
        self._ydl.close()

    def __enter__(self) -> PostSession:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class YtDlpBackend:
    IE_KEY = TikTokPhotosIE.ie_key()  # TikTok's extractor only, never yt-dlp's generic one
    USER_IE_KEY = TikTokPhotosUserIE.ie_key()  # yt-dlp's profile extractor, plus the photos of slideshow posts

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ffmpeg = shutil.which(settings.ffmpeg_path)
        self.ffmpeg_available = self.ffmpeg is not None
        self.max_bytes = settings.max_download_mb * 1024 * 1024
        if not self.ffmpeg_available:
            log.warning("ffmpeg not found (%s): MP3 conversion is disabled", settings.ffmpeg_path)

    # ------------------------------------------------------------------ lookup

    def _impersonation(self) -> dict[str, Any]:
        """yt-dlp's ``impersonate`` option for the one request that needs it (listing a profile), or nothing."""
        name = self.settings.impersonate.strip()
        if not name:
            return {}
        try:
            return {"impersonate": ImpersonateTarget.from_str(name)}
        except ValueError:
            log.warning("ignoring SNAPTIK_IMPERSONATE=%r: expected the name of a browser, such as 'chrome'", name)
            return {}

    def _new_ydl(self, **overrides: Any) -> yt_dlp.YoutubeDL:
        options = {
            "logger": _YdlLogger(),
            "quiet": True,
            "noprogress": True,
            "noplaylist": True,
            "ignoreconfig": True,
            "cachedir": False,
            "socket_timeout": self.settings.network_timeout,
            "retries": 2,
            "extractor_retries": 2,
            # We want every format, not yt-dlp's pick: we choose per download kind.
            "format": "all",
            "check_formats": False,
            "ignore_no_formats_error": True,
            "proxy": self.settings.proxy_url,
            **overrides,
        }
        try:
            ydl = yt_dlp.YoutubeDL(options)
        except YoutubeDLError as exc:
            if "impersonate" not in options:
                raise
            # The requested browser is not available (curl-cffi missing, or an unknown name): list without it.
            log.warning("browser impersonation is unavailable, so TikTok may refuse the request: %s", exc)
            del options["impersonate"]
            ydl = yt_dlp.YoutubeDL(options)
        ydl.add_info_extractor(TikTokPhotosIE())
        ydl.add_info_extractor(TikTokPhotosUserIE())
        return ydl

    def _extract(self, ydl: yt_dlp.YoutubeDL, url: str) -> Mapping[str, Any]:
        try:
            raw = ydl.extract_info(url, download=False, ie_key=self.IE_KEY)
        except DownloadError as exc:
            raise map_ytdlp_error(exc) from exc
        except Exception as exc:
            log.exception("unexpected failure while reading %s", url)
            raise UpstreamError() from exc
        if not raw:
            raise VideoUnavailable()
        return raw

    def fetch_info(self, url: str) -> VideoInfo:
        with self._new_ydl() as ydl:
            return parse_info(self._extract(ydl, url), url)

    def list_profile(self, handle: str, limit: int) -> ProfileListing:
        """The newest ``limit`` posts of a profile, with captions, dates and stats (no per-post page loads)."""
        pause = {"sleep_interval_requests": LISTING_PAUSE} if limit > LISTING_PAUSE_ABOVE else {}
        ydl = self._new_ydl(extractor_retries=LISTING_RETRIES, **pause, **self._impersonation())
        posts: list[ProfilePost] = []
        name: str | None = None
        more, warning = False, None
        try:
            raw = ydl.extract_info(
                f"https://www.tiktok.com/@{handle}", download=False, ie_key=self.USER_IE_KEY, process=False
            )
            if not isinstance(raw, Mapping):
                raise VideoUnavailable()
            try:
                # Pages are fetched lazily as we iterate, so a small limit costs only a request or two.
                for entry in raw.get("entries") or []:
                    if (post := parse_profile_entry(entry, handle)) is None:
                        continue
                    if len(posts) >= limit:
                        more = True
                        break
                    posts.append(post)
                    channel = entry.get("channel")
                    name = name or (channel if isinstance(channel, str) else None)
            except (DownloadError, ExtractorError) as exc:
                if not posts:
                    raise
                # A later page failed: the posts already collected are still worth showing.
                warning = f"TikTok stopped the list early. {_listing_error(exc).message}"
                more = True
        except (DownloadError, ExtractorError) as exc:
            raise _listing_error(exc) from exc
        except AppError:
            raise
        except Exception as exc:
            log.exception("unexpected failure while listing @%s", handle)
            raise UpstreamError() from exc
        finally:
            ydl.close()
        if not posts:
            raise VideoUnavailable("This account hasn't posted any public videos.")
        return ProfileListing(handle=handle, name=name, posts=posts, more=more, warning=warning)

    # ---------------------------------------------------------------- download

    def open_post(self, url: str) -> PostSession:
        ydl = self._new_ydl()
        try:
            info = parse_info(self._extract(ydl, url), url)
        except BaseException:
            ydl.close()
            raise
        return PostSession(self, ydl, info)

    def open_download(self, url: str, kind: str) -> Download:
        session = self.open_post(url)
        try:
            download = session.open(kind)
        except BaseException:
            session.close()
            raise
        download.add_cleanup(session.close)
        return download

    def _check_url(self, url: str) -> None:
        """Media URLs come from TikTok's responses, never from the client; still refuse local targets."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
            raise UpstreamError()
        if self.settings.allow_private_upstream:
            return
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise UpstreamError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return  # an ordinary DNS name
        if not address.is_global:
            raise UpstreamError()

    def _open_url(self, ydl: yt_dlp.YoutubeDL, url: str, headers: Mapping[str, str]) -> Any:
        self._check_url(url)
        # Same request shape as yt-dlp's own downloader: format headers + no compression
        # (so Content-Length matches the bytes we relay). Cookies come from ydl's jar.
        return ydl.urlopen(Request(url, headers={**headers, "Accept-Encoding": "identity"}))

    def _open_format(self, ydl: yt_dlp.YoutubeDL, fmt: MediaFormat) -> Any:
        try:
            return self._open_url(ydl, fmt.url, fmt.headers)
        except AppError:
            raise
        except Exception as exc:
            log.info("could not open format %s: %s", fmt.format_id, exc)
            raise UpstreamError("TikTok's servers wouldn't hand over the file. Please try again.") from exc

    def _stream(self, response: Any, *, filename: str, media_type: str) -> Download:
        length = _content_length(response)
        if length is not None and length > self.max_bytes:
            response.close()
            raise TooLarge()
        total = 0

        def read(size: int) -> bytes:
            nonlocal total
            chunk = response.read(size)
            total += len(chunk)
            if total > self.max_bytes:
                raise TooLarge()  # aborts the transfer: better a failed download than a truncated "complete" one
            return chunk

        return Download(filename=filename, media_type=media_type, length=length, reader=read, cleanups=[response.close])

    def _open_video(self, ydl: yt_dlp.YoutubeDL, info: VideoInfo, kind: str) -> Download:
        candidates = video_candidates(info, kind)
        if not candidates:
            raise FormatUnavailable()
        last_error: AppError | None = None
        for fmt in candidates[:MAX_FORMAT_ATTEMPTS]:  # TikTok lists several CDN URLs; some are dead
            try:
                response = self._open_format(ydl, fmt)
            except UpstreamError as exc:
                last_error = exc
                continue
            filename = safe_filename(download_stem(info, _VIDEO_SUFFIX.get(kind, "")), "mp4")
            return self._stream(response, filename=filename, media_type="video/mp4")
        raise last_error or UpstreamError()

    def _try_open_image(self, ydl: yt_dlp.YoutubeDL, url: str, stem: str) -> Download | None:
        """Open an image URL; ``None`` if it fails or is not a raster image."""
        try:
            response = self._open_url(ydl, url, {"Referer": TIKTOK_REFERER})
        except Exception as exc:  # noqa: BLE001 - the caller tries the next candidate
            log.info("could not open image: %s", exc)
            return None
        content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if content_type in _GENERIC_TYPES:  # a CDN that doesn't label its images: look at the file instead
            try:
                head = _read_head(response)
            except Exception as exc:  # noqa: BLE001 - the caller tries the next candidate
                log.info("could not read image: %s", exc)
                response.close()
                return None
            content_type = sniff_image(head) or ""
            response = _Prefixed(response, head)
        ext = _IMAGE_EXT.get(content_type)
        if ext is None:
            response.close()
            return None
        # The type we report is one of ours, never whatever the CDN claimed.
        return self._stream(response, filename=safe_filename(stem, ext), media_type=content_type)

    def _open_cover(self, ydl: yt_dlp.YoutubeDL, info: VideoInfo) -> Download:
        for url in info.covers[:MAX_FORMAT_ATTEMPTS]:
            if download := self._try_open_image(ydl, url, download_stem(info, "cover")):
                return download
        raise FormatUnavailable("This post has no cover image.")

    def _open_image(self, ydl: yt_dlp.YoutubeDL, info: VideoInfo, index: int) -> Download:
        if not 0 <= index < len(info.images):
            raise FormatUnavailable("This post has no such photo.")
        for url in info.images[index][:MAX_FORMAT_ATTEMPTS]:
            if download := self._try_open_image(ydl, url, download_stem(info, f"photo{index + 1:02d}")):
                return download
        raise FormatUnavailable(f"Photo {index + 1} of this post couldn't be downloaded.")

    def _read_image(self, ydl: yt_dlp.YoutubeDL, info: VideoInfo, index: int) -> tuple[str, bytes]:
        download = self._open_image(ydl, info, index)
        ext = download.filename.rsplit(".", 1)[-1]
        return f"{index + 1:02d}.{ext}", read_fully(download, MAX_PHOTO_BYTES)

    def _open_photos_zip(self, ydl: yt_dlp.YoutubeDL, info: VideoInfo) -> Download:
        """All photos of a slideshow as a streamed ZIP (plus the caption/details)."""
        if not info.images:
            raise FormatUnavailable("This post has no photos.")
        first = self._read_image(ydl, info, 0)  # fail with a real error before any bytes are sent

        def build() -> Iterator[bytes]:
            archive = ZipStream()
            skipped: list[int] = []
            try:
                for index in range(len(info.images)):
                    try:
                        name, data = first if index == 0 else self._read_image(ydl, info, index)
                    except AppError as exc:
                        log.info("photo %d of %s skipped: %s", index + 1, info.id, exc.message)
                        skipped.append(index + 1)
                        continue
                    yield from archive.add_bytes(name, data, info.timestamp)
                details = json.dumps(post_record(info), ensure_ascii=False, indent=2).encode()
                yield from archive.add_bytes("details.json", details, info.timestamp)
                if skipped:
                    note = ("These photos could not be downloaded: " + ", ".join(map(str, skipped)) + "\n").encode()
                    yield from archive.add_bytes("SKIPPED.txt", note, info.timestamp)
                yield from archive.finish()
            finally:
                archive.close()

        stream = build()
        return Download(
            filename=safe_filename(download_stem(info, "photos"), "zip"),
            media_type="application/zip",
            length=None,
            reader=IterReader(stream).read,
            cleanups=[stream.close],
        )

    def _open_meta(self, info: VideoInfo) -> Download:
        data = json.dumps(post_record(info), ensure_ascii=False, indent=2).encode()
        stream = io.BytesIO(data)
        return Download(
            filename=safe_filename(download_stem(info, "details"), "json"),
            media_type="application/json",
            length=len(data),
            reader=stream.read,
        )

    def _open_audio(self, ydl: yt_dlp.YoutubeDL, info: VideoInfo) -> Download:
        plan = audio_plan(info, ffmpeg_available=self.ffmpeg_available)
        if plan is None:
            raise FormatUnavailable("This post has no downloadable sound.")
        response = self._open_format(ydl, plan.source)
        if not plan.transcode:
            return self._stream(
                response,
                filename=safe_filename(download_stem(info, "audio"), plan.ext),
                media_type=_AUDIO_TYPES.get(plan.ext, "application/octet-stream"),
            )
        return self._convert_to_mp3(info, response)

    # ------------------------------------------------------------- conversion

    def _convert_to_mp3(self, info: VideoInfo, response: Any) -> Download:
        sweep_stale_tempdirs(self.settings.temp_dir)
        workdir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=self.settings.temp_dir))
        try:
            source, target = workdir / "source", workdir / "audio.mp3"
            self._save(response, source)
            self._run_ffmpeg(source, target, info)
            source.unlink(missing_ok=True)
            handle = target.open("rb")
        except BaseException:
            shutil.rmtree(workdir, ignore_errors=True)
            raise
        finally:
            response.close()
        return Download(
            filename=safe_filename(download_stem(info, "audio"), "mp3"),
            media_type="audio/mpeg",
            length=target.stat().st_size,
            reader=handle.read,
            cleanups=[handle.close, lambda: shutil.rmtree(workdir, ignore_errors=True)],
        )

    def _save(self, response: Any, path: Path) -> None:
        total = 0
        with path.open("wb") as out:
            while chunk := response.read(CHUNK_SIZE):
                total += len(chunk)
                if total > self.max_bytes:
                    raise TooLarge()
                out.write(chunk)

    def _run_ffmpeg(self, source: Path, target: Path, info: VideoInfo) -> None:
        command = [
            self.ffmpeg or "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source),
            "-vn", "-map", "0:a:0", "-map_metadata", "-1",
            "-c:a", "libmp3lame", "-b:a", "192k", "-id3v2_version", "3",
        ]  # fmt: skip
        if info.track:
            command += ["-metadata", f"title={_tag(info.track)}"]
        if info.artists:
            command += ["-metadata", f"artist={_tag(', '.join(info.artists))}"]
        command.append(str(target))
        try:
            result = subprocess.run(command, capture_output=True, timeout=self.settings.ffmpeg_timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ConversionFailed() from exc
        if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            stderr = result.stderr.decode("utf-8", "replace")[-500:]
            log.warning("ffmpeg failed (exit %s): %s", result.returncode, stderr)
            if "matches no streams" in stderr or "does not contain any stream" in stderr:
                raise FormatUnavailable("This video has no sound to extract.")
            raise ConversionFailed()
