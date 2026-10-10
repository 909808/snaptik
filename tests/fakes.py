"""Test doubles shared by the test-suite: realistic format lists and a stand-in backend."""
from __future__ import annotations

import io
from dataclasses import replace
from typing import Any

from app.backend import Download
from app.errors import AppError
from app.media import MediaFormat, VideoInfo

VIDEO_ID = "6718335390845095173"
CANONICAL = f"https://www.tiktok.com/@scout2015/video/{VIDEO_ID}"


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


class FakeBackend:
    """Implements the ``Backend`` protocol without touching the network."""

    def __init__(self, info: VideoInfo | None = None, *, payload: bytes = b"0123456789" * 100,
                 error: AppError | None = None, ffmpeg_available: bool = True) -> None:  # fmt: skip
        self.info = info or make_info()
        self.payload = payload
        self.error = error
        self.ffmpeg_available = ffmpeg_available
        self.fetch_calls: list[str] = []
        self.open_calls: list[tuple[str, str]] = []
        self.closed = 0

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

    def _count_close(self) -> None:
        self.closed += 1
