from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator

import pytest

from tests.helpers import Cdn


@pytest.fixture(scope="session")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, bytes]:
    """Small real media files, generated once per run with ffmpeg."""
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("ffmpeg/ffprobe not installed")
    folder = tmp_path_factory.mktemp("media")

    def ffmpeg(*args: str) -> None:
        subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, cwd=folder)

    video = ["-f", "lavfi", "-i", "testsrc=size=160x284:rate=15:duration=1"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    h264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    ffmpeg(*video, *tone, *h264, "-c:a", "aac", "-shortest", "-movflags", "+faststart", "video.mp4")
    ffmpeg(*video, *h264, "silent.mp4")
    ffmpeg(*tone, "-c:a", "aac", "sound.m4a")
    ffmpeg(*tone, "-c:a", "libmp3lame", "sound.mp3")
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=90x160", "-frames:v", "1", "cover.jpg")
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=64x96", "-frames:v", "1", "photo2.jpg")
    ffmpeg("-f", "lavfi", "-i", "smptebars=size=64x96", "-frames:v", "1", "photo3.png")
    return {p.name: p.read_bytes() for p in folder.iterdir()}


@pytest.fixture
def cdn(media: dict[str, bytes]) -> Iterator[Cdn]:
    server = Cdn(
        {
            "/video.mp4": (200, "video/mp4", media["video.mp4"]),
            "/video-sd.mp4": (200, "video/mp4", media["video.mp4"] + b"\x00sd"),
            "/video-wm.mp4": (200, "video/mp4", media["video.mp4"] + b"\x00wm"),
            "/silent.mp4": (200, "video/mp4", media["silent.mp4"]),
            "/garbage.mp4": (200, "video/mp4", b"this is not a media file" * 100),
            "/sound.m4a": (200, "audio/mp4", media["sound.m4a"]),
            "/sound.mp3": (200, "audio/mpeg", media["sound.mp3"]),
            "/cover.jpg": (200, "image/jpeg", media["cover.jpg"]),
            "/page.html": (200, "text/html", b"<html>not an image</html>"),
            "/photo1.jpg": (200, "image/jpeg", media["cover.jpg"]),
            "/photo2.jpg": (200, "image/jpeg", media["photo2.jpg"]),
            "/photo3.png": (200, "image/png", media["photo3.png"]),
            "/icon.svg": (200, "image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'/>"),
            "/dead.mp4": (404, "text/plain", b"gone"),
            "/big.bin": (200, "video/mp4", b"x" * (2 * 1024 * 1024)),
            "/nolength.bin": (200, "video/mp4", b"x" * (2 * 1024 * 1024)),
        }
    )
    yield server
    server.stop()
