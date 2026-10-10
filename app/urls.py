"""Turning whatever the user pasted into a validated TikTok target.

This is the app's trust boundary. Nothing downstream (the short-link resolver,
yt-dlp) ever receives the user's string: only a canonical URL that is rebuilt
from regex-validated components after passing through ``parse_tiktok_url``.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from .errors import InvalidURL, UpstreamError

MAX_URL_LENGTH = 2048
MAX_INPUT_LENGTH = 8192
MAX_REDIRECTS = 5

_DOMAINS = ("tiktok.com", "tiktokv.com")
_SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}

# TikTok's share sheet pastes whole sentences ("Check out ... https://vm.tiktok.com/ZM.../"),
# so we look for URLs inside free text instead of requiring the input to be only a URL.
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_BARE_URL_IN_TEXT = re.compile(
    r"(?<![\w@./-])(?:[a-z0-9-]+\.)*tiktok(?:v)?\.com/[^\s<>\"'`]+", re.IGNORECASE
)

_ID = r"(?P<id>\d{6,25})"
_VIDEO_PATH = re.compile(rf"^/@(?P<user>[A-Za-z0-9_.\-]{{0,64}})/(?:video|photo)/{_ID}/?$")
_ID_PATH = re.compile(rf"^/(?:embed(?:/v2)?|share/(?:video|photo)|v)/{_ID}(?:\.html)?/?$")
_SHORT_ROOT = re.compile(r"^/(?P<code>[A-Za-z0-9_\-]{3,40})/?$")
_SHORT_T = re.compile(r"^/t/(?P<code>[A-Za-z0-9_\-]{3,40})/?$")


@dataclass(frozen=True, slots=True)
class Target:
    """Either a concrete video (``video_id``) or a short link still to be resolved."""

    video_id: str | None = None
    user: str | None = None
    short_url: str | None = None

    @property
    def is_short(self) -> bool:
        return self.short_url is not None

    @property
    def canonical_url(self) -> str:
        """The one URL shape yt-dlp's TikTok extractor accepts (``www.`` host required)."""
        if self.video_id is None:
            raise ValueError("short links have no canonical URL until they are resolved")
        return f"https://www.tiktok.com/@{self.user or '_'}/video/{self.video_id}"


def _is_tiktok_host(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in _DOMAINS)


def parse_tiktok_url(raw: str) -> Target:
    """Validate a single URL and classify it. Raises ``InvalidURL`` for anything else."""
    raw = (raw or "").strip()
    if not raw or len(raw) > MAX_URL_LENGTH:
        raise InvalidURL()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise InvalidURL() from None

    if parts.scheme not in {"http", "https"}:
        raise InvalidURL()
    if parts.username is not None or parts.password is not None:
        raise InvalidURL()  # https://tiktok.com@evil.example/ and friends
    host = (parts.hostname or "").lower().rstrip(".")
    if not host.isascii() or not _is_tiktok_host(host):
        raise InvalidURL()
    if port not in (None, 80, 443):
        raise InvalidURL()

    path = parts.path or "/"
    if match := _VIDEO_PATH.match(path):
        return Target(video_id=match["id"], user=match["user"] or None)
    if match := _ID_PATH.match(path):
        return Target(video_id=match["id"])
    if _SHORT_T.match(path) or (host in _SHORT_HOSTS and _SHORT_ROOT.match(path)):
        return Target(short_url=f"https://{host}{path}")
    raise InvalidURL()


def _candidates(text: str) -> Iterator[str]:
    for match in _URL_IN_TEXT.finditer(text):
        yield match.group(0).rstrip(".,;:!?)]}>'\"")
    for match in _BARE_URL_IN_TEXT.finditer(text):
        yield "https://" + match.group(0).rstrip(".,;:!?)]}>'\"")


def parse_input(text: str) -> Target:
    """Find the first TikTok video link inside free-form pasted text."""
    text = (text or "").strip()
    if not text or len(text) > MAX_INPUT_LENGTH:
        raise InvalidURL()
    first_error: InvalidURL | None = None
    for candidate in _candidates(text):
        try:
            return parse_tiktok_url(candidate)
        except InvalidURL as exc:
            first_error = first_error or exc
    raise first_error or InvalidURL()


def _follow_redirect(url: str, client: httpx.Client) -> str:
    try:
        with client.stream("GET", url) as response:  # headers only; the body is never read
            status = response.status_code
            location = response.headers.get("location")
    except httpx.HTTPError as exc:
        raise UpstreamError("Couldn't open that short link. Try the full video URL instead.") from exc
    if status not in _REDIRECT_STATUSES or not location:
        raise UpstreamError("Couldn't resolve that short link. Try the full video URL instead.")
    return urljoin(url, location)


def resolve_target(text: str, client: httpx.Client) -> Target:
    """Parse the input and follow short links (vm./vt./``/t/``) to a concrete video.

    Redirects are followed by hand so that every hop is re-validated: a short link
    that redirects anywhere outside TikTok is rejected rather than fetched.
    """
    target = parse_input(text)
    hops = 0
    while target.is_short:
        if hops >= MAX_REDIRECTS:
            raise InvalidURL("That link redirects too many times.")
        target = parse_tiktok_url(_follow_redirect(target.short_url, client))  # type: ignore[arg-type]
        hops += 1
    return target
