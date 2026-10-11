"""Test doubles shared by the test-suite: realistic format lists and a stand-in backend."""
from __future__ import annotations

import io
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from app.backend import Download
from app.errors import AppError
from app.media import MediaFormat, ProfileListing, ProfilePost, VideoInfo

VIDEO_ID = "6718335390845095173"
CANONICAL = f"https://www.tiktok.com/@scout2015/video/{VIDEO_ID}"
BASE_ID = 7_300_000_000_000_000_000
BASE_TIME = 1_700_000_000  # 2023-11-14 22:13:20 UTC


def fmt(format_id: str, *, w: int | None = None, h: int | None = None, vcodec: str = "h264", acodec: str = "aac",
        tbr: float | None = None, filesize: int | None = None, note: str = "", ext: str = "mp4",
        url: str | None = None, headers: dict[str, str] | None = None) -> MediaFormat:  # fmt: skip
    return MediaFormat(
        format_id=format_id,
        url=url or f"https://cdn.example.test/{format_id}.mp4",
        ext=ext,
        vcodec=vcodec,
        acodec=acodec,
        width=w,
        height=h,
        tbr=tbr,
        filesize=filesize,
        note=note,
        headers=headers or {},
    )


def sample_formats() -> list[MediaFormat]:
    """What yt-dlp's web extractor returns for a typical post (see the docstring in app/media.py)."""
    return [
        fmt("h264_540p_900000", w=576, h=1024, tbr=900, filesize=2_400_000),
        fmt("h264_1080p_2500000", w=1080, h=1920, tbr=2500, filesize=6_900_000),
        fmt("bytevc1_1080p_1600000", w=1080, h=1920, vcodec="h265", tbr=1600, filesize=4_300_000),
        fmt("bytevc2_1080p_1000000", w=1080, h=1920, vcodec="bytevc2", tbr=1000, note="UNPLAYABLE"),
        fmt("download", w=576, h=1024, note="watermarked"),
        fmt("audio", vcodec="none", acodec="aac", ext="m4a", url="https://cdn.example.test/audio.m4a"),
    ]


def make_info(**overrides: Any) -> VideoInfo:
    info = VideoInfo(
        id=VIDEO_ID,
        url=CANONICAL,
        title="Hello from the test suite #fyp",
        description="Hello from the test suite #fyp",
        author_name="Scout",
        author_handle="scout2015",
        duration=15,
        thumbnail="https://cdn.example.test/cover.jpg",
        covers=["https://cdn.example.test/cover.jpg", "https://cdn.example.test/origin.jpg"],
        stats={"views": 1_234_567, "likes": 84_200, "comments": 1_290, "shares": 3_410},
        track="original sound - scout2015",
        artists=["Scout"],
        formats=sample_formats(),
    )
    return replace(info, **overrides)


def post_id(n: int) -> str:
    return str(BASE_ID + n)


def post_url(n: int, handle: str = "scout2015") -> str:
    return f"https://www.tiktok.com/@{handle}/video/{post_id(n)}"


def make_post_info(n: int, **overrides: Any) -> VideoInfo:
    """The n-th post of the fake profile: its own id, date and caption (post 1 is the oldest)."""
    caption = f"caption of post {n} #tag{n}"
    fields: dict[str, Any] = {
        "id": post_id(n), "url": post_url(n), "title": caption, "description": caption, "caption": caption,
        "timestamp": BASE_TIME + n * 86_400,
    }  # fmt: skip
    return make_info(**{**fields, **overrides})


def make_listing(count: int = 5, *, handle: str = "scout2015", more: bool = False, warning: str | None = None) -> ProfileListing:
    posts = [
        ProfilePost(
            id=post_id(n), url=post_url(n, handle), caption=f"caption of post {n} #tag{n}",
            timestamp=BASE_TIME + n * 86_400, duration=15, thumbnail=f"https://cdn.example.test/{n}.jpg",
            stats={"views": 1000 * n, "likes": 10 * n, "comments": n, "shares": 1},
        )
        for n in range(count, 0, -1)  # newest first, like TikTok
    ]  # fmt: skip
    return ProfileListing(handle=handle, name="Scout", posts=posts, more=more, warning=warning)


_EXT = {"video": "mp4", "video_sd": "mp4", "video_wm": "mp4", "video_h264": "mp4", "audio": "mp3", "cover": "jpg",
        "meta": "json", "images": "zip"}


class FakePost:
    """Stand-in for ``backend.Post``: one looked-up post whose files are served from memory."""

    def __init__(self, backend: FakeBackend, info: VideoInfo) -> None:
        self._backend = backend
        self.info = info

    def open(self, kind: str) -> Download:
        self._backend.opened.append((self.info.id, kind))
        self._backend._fail(self.info.id, kind)
        return self._backend._download(self.info.id, kind, f"fake_{kind}.{_EXT[kind]}")

    def open_image(self, index: int) -> Download:
        self._backend.opened.append((self.info.id, f"image:{index}"))
        self._backend._fail(self.info.id, f"image:{index}")
        return self._backend._download(self.info.id, f"image:{index}", f"fake_photo.{'png' if index % 2 else 'jpg'}")

    def close(self) -> None:
        self._backend.sessions_closed += 1

    def __enter__(self) -> FakePost:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FakeBackend:
    """Implements the ``Backend`` protocol without touching the network.

    ``failures`` scripts errors: ``{(post_id, stage): [error, ...]}`` raises each error in turn at that
    stage ("lookup", a kind such as "video", or "image:0") and succeeds once the list is used up.
    """

    def __init__(self, info: VideoInfo | None = None, *, payload: bytes = b"0123456789" * 100,
                 error: AppError | None = None, ffmpeg_available: bool = True,
                 posts: Mapping[str, VideoInfo] | None = None, listing: ProfileListing | None = None,
                 failures: Mapping[tuple[str, str], Sequence[AppError]] | None = None,
                 short_reads: set[tuple[str, str]] | None = None,
                 on_lookup: Callable[[str], None] | None = None) -> None:  # fmt: skip
        self.info = info or make_info()
        self.payload = payload
        self.error = error
        self.ffmpeg_available = ffmpeg_available
        self.posts = dict(posts or {})
        self.listing = listing
        self.failures = {key: list(errors) for key, errors in (failures or {}).items()}
        self.short_reads = short_reads or set()
        self.on_lookup = on_lookup  # called with the post id at the start of every lookup (to script timing)
        self.fetch_calls: list[str] = []
        self.open_calls: list[tuple[str, str]] = []
        self.list_calls: list[tuple[str, int]] = []
        self.lookups: list[str] = []
        self.opened: list[tuple[str, str]] = []
        self.closed = 0
        self.sessions_opened = 0
        self.sessions_closed = 0

    # -- single post
    def fetch_info(self, url: str) -> VideoInfo:
        self.fetch_calls.append(url)
        if self.error:
            raise self.error
        return self.info

    def open_download(self, url: str, kind: str) -> Download:
        self.open_calls.append((url, kind))
        if self.error:
            raise self.error
        stream = io.BytesIO(self.payload)
        return Download(
            filename=f"fake_{kind}.mp4",
            media_type="video/mp4",
            length=len(self.payload),
            reader=stream.read,
            cleanups=[self._count_close],
        )

    # -- several posts
    def open_post(self, url: str) -> FakePost:
        found = re.search(r"/video/(\d+)", url)
        pid = found.group(1) if found else self.info.id
        self.lookups.append(pid)
        if self.on_lookup:
            self.on_lookup(pid)
        if self.error:
            raise self.error
        self._fail(pid, "lookup")
        info = self.posts.get(pid)
        if info is None:
            info = replace(self.info, id=pid, url=url, description=f"post {pid}", caption=f"post {pid}", title=f"post {pid}")
        self.sessions_opened += 1
        return FakePost(self, info)

    def list_profile(self, handle: str, limit: int) -> ProfileListing:
        self.list_calls.append((handle, limit))
        if self.error:
            raise self.error
        listing = self.listing or make_listing(handle=handle)
        return replace(listing, posts=listing.posts[:limit], more=listing.more or len(listing.posts) > limit)

    # -- helpers
    def body(self, pid: str, stage: str) -> bytes:
        return f"[{stage} of {pid}]".encode() * 40

    def _fail(self, pid: str, stage: str) -> None:
        queue = self.failures.get((pid, stage))
        if queue:
            raise queue.pop(0)

    def _download(self, pid: str, stage: str, filename: str) -> Download:
        data = self.body(pid, stage)
        stream = io.BytesIO(data)
        length = len(data) + 10 if (pid, stage) in self.short_reads else len(data)  # announces more than it sends
        return Download(filename=filename, media_type="application/octet-stream", length=length,
                        reader=stream.read, cleanups=[self._count_close])  # fmt: skip

    def _count_close(self) -> None:
        self.closed += 1
