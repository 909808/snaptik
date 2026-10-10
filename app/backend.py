"""yt-dlp integration: look a post up, then stream the chosen file to the client.

The rest of the app only knows the ``Backend`` protocol, so tests can swap in a
fake and this file stays the single place that talks to TikTok.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.networking import Request
from yt_dlp.utils import DownloadError

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
    VIDEO_KINDS,
    MediaFormat,
    VideoInfo,
    audio_plan,
    download_stem,
    parse_info,
    safe_filename,
    video_candidates,
)

log = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024
MAX_FORMAT_ATTEMPTS = 4  # CDN URLs that fail to open before we give up
TEMP_PREFIX = "snaptik-"

_AUDIO_TYPES = {"mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac", "ogg": "audio/ogg", "opus": "audio/ogg"}
# Raster formats only: never relay something like SVG that could carry script under our origin.
_IMAGE_EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/avif": "avif", "image/gif": "gif"}


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


class Backend(Protocol):
    ffmpeg_available: bool

    def fetch_info(self, url: str) -> VideoInfo: ...

    def open_download(self, url: str, kind: str) -> Download: ...


# Phrases in yt-dlp / TikTok error messages, checked in this order (see yt_dlp/extractor/tiktok.py).
_BLOCKED = ("ip address is blocked", "challenge", "captcha", "http error 403", "http error 429", "too many requests")
_LOGIN = ("requiring login", "log in", "login", "permission to view", "comfortable for some audiences")
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
    if any(phrase in text for phrase in _GONE):
        return VideoUnavailable()
    return UpstreamError()  # includes plain network failures, and "Service Unavailable" is deliberately not "gone"


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


class YtDlpBackend:
    IE_KEY = "TikTok"  # extract with TikTok's extractor only, never yt-dlp's generic one

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.ffmpeg = shutil.which(settings.ffmpeg_path)
        self.ffmpeg_available = self.ffmpeg is not None
        self.max_bytes = settings.max_download_mb * 1024 * 1024
        if not self.ffmpeg_available:
            log.warning("ffmpeg not found (%s): MP3 conversion is disabled", settings.ffmpeg_path)

    # ------------------------------------------------------------------ lookup

    def _new_ydl(self) -> yt_dlp.YoutubeDL:
        return yt_dlp.YoutubeDL(
            {
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
            }
        )

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

    # ---------------------------------------------------------------- download

    def open_download(self, url: str, kind: str) -> Download:
        # The same YoutubeDL instance does the lookup and the transfer, because it
        # holds the cookies and headers TikTok's CDN expects.
        ydl = self._new_ydl()
        try:
            info = parse_info(self._extract(ydl, url), url)
            if kind == KIND_COVER:
                download = self._open_cover(ydl, info)
            elif kind == KIND_AUDIO:
                download = self._open_audio(ydl, info)
            elif kind in VIDEO_KINDS:
                download = self._open_video(ydl, info, kind)
            else:
                raise FormatUnavailable()
        except BaseException:
            ydl.close()
            raise
        download.add_cleanup(ydl.close)
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
            return self._stream(response, filename=safe_filename(download_stem(info), "mp4"), media_type="video/mp4")
        raise last_error or UpstreamError()

    def _open_cover(self, ydl: yt_dlp.YoutubeDL, info: VideoInfo) -> Download:
        for url in info.covers[:MAX_FORMAT_ATTEMPTS]:
            try:
                response = self._open_url(ydl, url, {"Referer": "https://www.tiktok.com/"})
            except Exception as exc:  # noqa: BLE001 - try the next candidate
                log.info("could not open cover: %s", exc)
                continue
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            ext = _IMAGE_EXT.get(content_type)
            if ext is None:
                response.close()
                continue
            return self._stream(
                response, filename=safe_filename(download_stem(info, "cover"), ext), media_type=content_type
            )
        raise FormatUnavailable("This post has no cover image.")

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
