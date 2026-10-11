"""Integration tests for the yt-dlp backend.

Real yt-dlp (its extraction pipeline and its HTTP client) and real ffmpeg run here.
Only TikTok itself is replaced: by a stand-in extractor that returns TikTok-shaped
format lists (see the docstring of app/media.py), and by a local HTTP server that
plays the CDN. What this does NOT prove is that TikTok's live site still matches
yt-dlp's extractor; that is yt-dlp's job and needs real network access.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import DownloadError, ExtractorError

from app.backend import YtDlpBackend, map_ytdlp_error, sniff_image, sweep_stale_tempdirs
from app.config import Settings
from app.errors import (
    ConversionFailed,
    FormatUnavailable,
    LoginRequired,
    TooLarge,
    UpstreamBlocked,
    UpstreamError,
    VideoUnavailable,
)
from tests.fakes import CANONICAL
from tests.helpers import Cdn, needs_ffmpeg, read_all

# ------------------------------------------------------- stand-in for TikTok itself


def video_format(cdn: Cdn, path: str, format_id: str, **fields: Any) -> dict[str, Any]:
    return {"format_id": format_id, "url": cdn.url + path, "ext": "mp4", "vcodec": "h264", "acodec": "aac", **fields}


def standard_formats(cdn: Cdn) -> list[dict[str, Any]]:
    return [
        video_format(cdn, "/video-sd.mp4", "h264_540p_900000", width=576, height=1024, tbr=900),
        video_format(cdn, "/video.mp4", "h264_1080p_2500000", width=1080, height=1920, tbr=2500),
        video_format(cdn, "/video-wm.mp4", "download", width=576, height=1024, format_note="watermarked"),
        {"format_id": "audio", "url": cdn.url + "/sound.m4a", "ext": "m4a", "vcodec": "none", "acodec": "aac"},
    ]


def tiktok_like(cdn: Cdn, formats: list[dict[str, Any]] | None = None, **overrides: Any) -> dict[str, Any]:
    return {
        "title": "cute cat video",
        "description": "cute cat video #cats",
        "channel": "Scout",
        "uploader": "scout2015",
        "duration": 1,
        "track": "original sound",
        "artists": ["Scout"],
        "http_headers": {"Referer": CANONICAL},
        "thumbnails": [
            {"id": "dynamicCover", "url": cdn.url + "/page.html", "preference": -2},
            {"id": "cover", "url": cdn.url + "/cover.jpg", "preference": -1},
        ],
        "formats": formats if formats is not None else standard_formats(cdn),
        **overrides,
    }


def make_backend(result: dict[str, Any], *, failure: Exception | None = None, **settings: Any) -> YtDlpBackend:
    class StandInTikTokIE(InfoExtractor):
        IE_NAME = "standin"
        _VALID_URL = r"https?://www\.tiktok\.com/@[\w.-]+/video/(?P<id>\d+)"

        def _real_extract(self, url: str) -> dict[str, Any]:
            if failure is not None:
                raise failure
            return {"id": self._match_id(url), **result}

    class Backend(YtDlpBackend):
        IE_KEY = StandInTikTokIE.ie_key()

        def _new_ydl(self, **overrides):  # type: ignore[no-untyped-def]
            ydl = super()._new_ydl(**overrides)
            ydl.add_info_extractor(StandInTikTokIE())
            return ydl

    return Backend(Settings(**{"allow_private_upstream": True, "network_timeout": 5, **settings}))


def probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,codec_type:format=duration:format_tags=title,artist",
         "-of", "json", str(path)],
        capture_output=True, check=True, text=True,
    )  # fmt: skip
    return json.loads(out.stdout)


# ---------------------------------------------------------------------- lookup


def test_lookup_goes_through_the_real_yt_dlp_pipeline(cdn: Cdn) -> None:
    info = make_backend(tiktok_like(cdn)).fetch_info(CANONICAL)
    assert (info.id, info.author_handle, info.author_name) == ("6718335390845095173", "scout2015", "Scout")
    assert {f.format_id for f in info.formats} == {"h264_540p_900000", "h264_1080p_2500000", "download", "audio"}
    assert info.covers[0].endswith("/cover.jpg")  # the static cover outranks the animated one
    # yt-dlp merged its standard headers with the extractor's Referer for every format:
    headers = {k.lower(): v for k, v in info.formats[0].headers.items()}
    assert headers["referer"] == CANONICAL and "user-agent" in headers
    assert "cookie" not in headers


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("Your IP address is blocked from accessing this post", UpstreamBlocked),
        ("TikTok is requiring login for access to this content", LoginRequired),
        ("You do not have permission to view this post. Log into an account that has access", LoginRequired),
        ("Video not available, status code 10231", VideoUnavailable),
        ("Something nobody has seen before", UpstreamError),
    ],
)
def test_extractor_failures_become_user_facing_errors(cdn: Cdn, message: str, error: type[Exception]) -> None:
    backend = make_backend(tiktok_like(cdn), failure=ExtractorError(message, expected=True))
    with pytest.raises(error) as caught:
        backend.fetch_info(CANONICAL)
    assert type(caught.value) is error


def test_a_crashing_extractor_is_an_upstream_error_not_a_stack_trace(cdn: Cdn) -> None:
    with pytest.raises(UpstreamError):
        make_backend(tiktok_like(cdn), failure=RuntimeError("boom")).fetch_info(CANONICAL)


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("ERROR: [TikTok] 1: Unable to download webpage: HTTP Error 404: Not Found", VideoUnavailable),
        ("ERROR: [TikTok] 1: Unable to download webpage: HTTP Error 403: Forbidden", UpstreamBlocked),
        ("ERROR: [TikTok] 1: Unable to download webpage: HTTP Error 429: Too Many Requests", UpstreamBlocked),
        ("ERROR: [TikTok] 1: Unable to download webpage: <urlopen error [Errno -2] Name or service not known>", UpstreamError),
        ("ERROR: [TikTok] 1: Unable to extract universal data for rehydration", UpstreamError),
        ("ERROR: [TikTok] 1: Unable to extract challenge cookie name", UpstreamBlocked),
        ("This post may not be comfortable for some audiences. Log in for access", LoginRequired),
        ("This video is private", VideoUnavailable),
        ("Service Unavailable", UpstreamError),  # "unavailable" alone must not read as "video removed"
    ],
)  # fmt: skip
def test_error_message_mapping(message: str, error: type[Exception]) -> None:
    wrapped = DownloadError(message, exc_info=(ExtractorError, ExtractorError(message), None))
    assert type(map_ytdlp_error(wrapped)) is error
    assert type(map_ytdlp_error(ExtractorError(message))) is error


# -------------------------------------------------------------------- video


def test_video_is_streamed_byte_for_byte_with_the_headers_tiktok_expects(cdn: Cdn, media: dict[str, bytes]) -> None:
    download = make_backend(tiktok_like(cdn)).open_download(CANONICAL, "video")
    try:
        assert read_all(download) == media["video.mp4"]
        assert download.length == len(media["video.mp4"])
        assert (download.media_type, download.filename) == ("video/mp4", "tiktok_scout2015_6718335390845095173.mp4")
    finally:
        download.close()
    request = next(r for r in cdn.requests if r["path"] == "/video.mp4")
    assert request["headers"]["referer"] == CANONICAL
    assert request["headers"]["accept-encoding"] == "identity"  # so Content-Length matches the relayed bytes
    assert request["headers"]["user-agent"]


@pytest.mark.parametrize(
    ("kind", "path"), [("video", "/video.mp4"), ("video_sd", "/video-sd.mp4"), ("video_wm", "/video-wm.mp4")]
)
def test_each_video_kind_fetches_its_own_rendition(cdn: Cdn, kind: str, path: str) -> None:
    download = make_backend(tiktok_like(cdn)).open_download(CANONICAL, kind)
    download.close()
    assert cdn.fetched() == [path]


def test_a_dead_cdn_url_falls_back_to_the_next_rendition(cdn: Cdn, media: dict[str, bytes]) -> None:
    formats = [
        video_format(cdn, "/dead.mp4", "h264_1080p_2500000", width=1080, height=1920, tbr=2500),
        video_format(cdn, "/video.mp4", "h264_720p_1500000", width=720, height=1280, tbr=1500),
    ]
    download = make_backend(tiktok_like(cdn, formats)).open_download(CANONICAL, "video")
    try:
        assert read_all(download) == media["video.mp4"]
    finally:
        download.close()
    assert cdn.fetched() == ["/dead.mp4", "/video.mp4"]


def test_when_every_url_is_dead_the_error_is_an_upstream_error(cdn: Cdn) -> None:
    formats = [video_format(cdn, "/dead.mp4", f"h264_{n}", width=576, height=1024, tbr=n) for n in (900, 800, 700)]
    with pytest.raises(UpstreamError):
        make_backend(tiktok_like(cdn, formats)).open_download(CANONICAL, "video")


def test_unknown_or_unavailable_kinds_are_refused(cdn: Cdn) -> None:
    backend = make_backend(tiktok_like(cdn))
    for kind in ("nonsense", ""):
        with pytest.raises(FormatUnavailable):
            backend.open_download(CANONICAL, kind)
    only_one = make_backend(tiktok_like(cdn, [video_format(cdn, "/video.mp4", "only", width=576, height=1024)]))
    with pytest.raises(FormatUnavailable):
        only_one.open_download(CANONICAL, "video_sd")


# ----------------------------------------------------------------------- limits


def test_a_file_that_announces_itself_as_too_large_is_refused_before_any_bytes_are_sent(cdn: Cdn) -> None:
    formats = [video_format(cdn, "/big.bin", "h264_big", width=576, height=1024)]
    with pytest.raises(TooLarge):
        make_backend(tiktok_like(cdn, formats), max_download_mb=1).open_download(CANONICAL, "video")


def test_a_file_that_lies_about_its_size_is_cut_off_mid_stream(cdn: Cdn) -> None:
    formats = [video_format(cdn, "/nolength.bin", "h264_big", width=576, height=1024)]
    download = make_backend(tiktok_like(cdn, formats), max_download_mb=1).open_download(CANONICAL, "video")
    try:
        assert download.length is None
        with pytest.raises(TooLarge):
            read_all(download)
    finally:
        download.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x", "http://127.1.2.3:8080/x", "http://localhost/x", "http://api.localhost/x",
        "http://10.0.0.5/x", "http://192.168.1.1/x", "http://172.16.0.1/x", "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/x", "http://[fd00::1]/x", "http://0.0.0.0/x", "http://printer.local/x", "http://db.internal/x",
        "ftp://example.com/x", "file:///etc/passwd", "https://user:pw@example.com/x", "https:///nohost",
    ],
)  # fmt: skip
def test_media_urls_pointing_at_internal_targets_are_refused(url: str) -> None:
    backend = YtDlpBackend(Settings())
    with pytest.raises(UpstreamError):
        backend._check_url(url)


@pytest.mark.parametrize("url", ["https://v16-webapp-prime.tiktok.com/video/tos/x", "https://93.184.216.34/x", "http://cdn.example/x"])
def test_ordinary_media_urls_are_accepted(url: str) -> None:
    YtDlpBackend(Settings())._check_url(url)


def test_the_guard_is_applied_end_to_end(cdn: Cdn) -> None:
    backend = make_backend(tiktok_like(cdn), allow_private_upstream=False)
    with pytest.raises(UpstreamError):
        backend.open_download(CANONICAL, "video")
    assert cdn.requests == []  # nothing was ever requested from the local server


# ------------------------------------------------------------------------ audio


def temp_dirs(folder: Path) -> list[Path]:
    return list(folder.glob("snaptik-*"))


@needs_ffmpeg
def test_native_m4a_track_is_converted_to_a_tagged_mp3(cdn: Cdn, tmp_path: Path) -> None:
    backend = make_backend(tiktok_like(cdn), temp_dir=str(tmp_path))
    download = backend.open_download(CANONICAL, "audio")
    try:
        assert (download.media_type, download.filename) == ("audio/mpeg", "tiktok_scout2015_6718335390845095173_audio.mp3")
        data = read_all(download)
        assert len(data) == download.length
        assert temp_dirs(tmp_path), "the converted file lives in a temp dir while it is being sent"
    finally:
        download.close()
    assert temp_dirs(tmp_path) == []  # ...and is gone once the transfer ends

    out = tmp_path / "result.mp3"
    out.write_bytes(data)
    info = probe(out)
    assert [(s["codec_type"], s["codec_name"]) for s in info["streams"]] == [("audio", "mp3")]
    assert 0.8 < float(info["format"]["duration"]) < 1.3
    assert info["format"]["tags"] == {"title": "original sound", "artist": "Scout"}
    assert cdn.fetched() == ["/sound.m4a"]  # the post's own sound track, not the video


@needs_ffmpeg
def test_native_mp3_is_passed_through_untouched(cdn: Cdn, media: dict[str, bytes], tmp_path: Path) -> None:
    formats = [{"format_id": "audio", "url": cdn.url + "/sound.mp3", "ext": "mp3", "vcodec": "none", "acodec": "mp3"}]
    download = make_backend(tiktok_like(cdn, formats), temp_dir=str(tmp_path)).open_download(CANONICAL, "audio")
    try:
        assert read_all(download) == media["sound.mp3"]
        assert download.media_type == "audio/mpeg" and download.filename.endswith(".mp3")
    finally:
        download.close()
    assert temp_dirs(tmp_path) == []  # no conversion, so no temp files


@needs_ffmpeg
def test_without_a_sound_track_the_audio_is_pulled_out_of_the_video(cdn: Cdn, tmp_path: Path) -> None:
    formats = [video_format(cdn, "/video.mp4", "h264_540p_900000", width=576, height=1024, tbr=900)]
    download = make_backend(tiktok_like(cdn, formats), temp_dir=str(tmp_path)).open_download(CANONICAL, "audio")
    try:
        out = tmp_path / "from-video.mp3"
        out.write_bytes(read_all(download))
    finally:
        download.close()
    assert [s["codec_name"] for s in probe(out)["streams"]] == ["mp3"]


def test_without_ffmpeg_the_original_m4a_is_delivered(cdn: Cdn, media: dict[str, bytes]) -> None:
    backend = make_backend(tiktok_like(cdn), ffmpeg_path="/nonexistent/ffmpeg")
    assert not backend.ffmpeg_available
    download = backend.open_download(CANONICAL, "audio")
    try:
        assert read_all(download) == media["sound.m4a"]
        assert (download.media_type, download.filename.rsplit(".", 1)[1]) == ("audio/mp4", "m4a")
    finally:
        download.close()


def test_without_ffmpeg_and_without_a_sound_track_there_is_nothing_to_offer(cdn: Cdn) -> None:
    formats = [video_format(cdn, "/video.mp4", "h264_540p_900000", width=576, height=1024)]
    backend = make_backend(tiktok_like(cdn, formats), ffmpeg_path="/nonexistent/ffmpeg")
    with pytest.raises(FormatUnavailable):
        backend.open_download(CANONICAL, "audio")


@needs_ffmpeg
def test_a_silent_video_reports_that_there_is_no_sound(cdn: Cdn, tmp_path: Path) -> None:
    formats = [video_format(cdn, "/silent.mp4", "h264_540p_900000", width=576, height=1024)]
    with pytest.raises(FormatUnavailable, match="no sound"):
        make_backend(tiktok_like(cdn, formats), temp_dir=str(tmp_path)).open_download(CANONICAL, "audio")
    assert temp_dirs(tmp_path) == []


@needs_ffmpeg
def test_a_corrupt_source_fails_cleanly_and_leaves_nothing_behind(cdn: Cdn, tmp_path: Path) -> None:
    formats = [video_format(cdn, "/garbage.mp4", "h264_540p_900000", width=576, height=1024)]
    with pytest.raises(ConversionFailed):
        make_backend(tiktok_like(cdn, formats), temp_dir=str(tmp_path)).open_download(CANONICAL, "audio")
    assert temp_dirs(tmp_path) == []


@needs_ffmpeg
def test_an_oversized_source_is_rejected_and_cleaned_up(cdn: Cdn, tmp_path: Path) -> None:
    formats = [video_format(cdn, "/nolength.bin", "h264_540p_900000", width=576, height=1024)]
    backend = make_backend(tiktok_like(cdn, formats), temp_dir=str(tmp_path), max_download_mb=1)
    with pytest.raises(TooLarge):
        backend.open_download(CANONICAL, "audio")
    assert temp_dirs(tmp_path) == []


@needs_ffmpeg
def test_ffmpeg_that_hangs_is_cut_off(cdn: Cdn, tmp_path: Path) -> None:
    slow = tmp_path / "slow-ffmpeg"
    slow.write_text("#!/bin/sh\nsleep 30\n")
    slow.chmod(0o755)
    backend = make_backend(tiktok_like(cdn), ffmpeg_path=str(slow), ffmpeg_timeout=1, temp_dir=str(tmp_path))
    started = time.monotonic()
    with pytest.raises(ConversionFailed):
        backend.open_download(CANONICAL, "audio")
    assert time.monotonic() - started < 10
    assert temp_dirs(tmp_path) == []


# ------------------------------------------------------------------------ cover


def test_cover_skips_candidates_that_are_not_images(cdn: Cdn, media: dict[str, bytes]) -> None:
    # The animated cover (an HTML error page here) is ranked after the static one, so force it first.
    result = tiktok_like(cdn, thumbnails=[
        {"id": "a", "url": cdn.url + "/page.html", "preference": 1},
        {"id": "b", "url": cdn.url + "/dead.mp4", "preference": 0},
        {"id": "c", "url": cdn.url + "/cover.jpg", "preference": -1},
    ])  # fmt: skip
    download = make_backend(result).open_download(CANONICAL, "cover")
    try:
        assert read_all(download) == media["cover.jpg"]
        assert (download.media_type, download.filename) == ("image/jpeg", "tiktok_scout2015_6718335390845095173_cover.jpg")
    finally:
        download.close()
    assert cdn.fetched() == ["/page.html", "/dead.mp4", "/cover.jpg"]


def test_only_raster_images_are_relayed_as_covers(cdn: Cdn, media: dict[str, bytes]) -> None:
    result = tiktok_like(cdn, thumbnails=[
        {"id": "a", "url": cdn.url + "/icon.svg", "preference": 1},  # could carry script: never relayed
        {"id": "b", "url": cdn.url + "/cover.jpg", "preference": 0},
    ])  # fmt: skip
    download = make_backend(result).open_download(CANONICAL, "cover")
    try:
        assert (read_all(download), download.media_type) == (media["cover.jpg"], "image/jpeg")
    finally:
        download.close()
    assert cdn.fetched() == ["/icon.svg", "/cover.jpg"]


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"\xff\xd8\xff\xe0" + b"\0" * 12, "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n" + b"\0" * 8, "image/png"),
        (b"RIFF\x24\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"GIF87a" + b"\0" * 10, "image/gif"),
        (b"GIF89a" + b"\0" * 10, "image/gif"),
        (b"\x00\x00\x00\x1cftypavif" + b"\0" * 4, "image/avif"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'", None),  # script in disguise
        (b"<!doctype html><html>", None),
        (b"RIFF\x24\x00\x00\x00WAVEfmt ", None),  # a RIFF file that is not an image
        (b"\x00\x00\x00\x18ftypmp42" + b"\0" * 4, None),  # a video
        (b"\xff\xd8", None),  # too short to be sure
        (b"", None),
    ],
)
def test_images_are_recognised_by_their_first_bytes(head: bytes, expected: str | None) -> None:
    assert sniff_image(head) == expected


@pytest.mark.parametrize(
    ("path", "ext", "media_type", "key"),
    [("/cover-unlabelled.jpg", "jpg", "image/jpeg", "cover.jpg"), ("/photo3-unlabelled.png", "png", "image/png", "photo3.png")],
)
def test_an_image_the_cdn_does_not_label_is_recognised_and_arrives_whole(
    cdn: Cdn, media: dict[str, bytes], path: str, ext: str, media_type: str, key: str
) -> None:
    download = make_backend(tiktok_like(cdn, thumbnails=[{"id": "a", "url": cdn.url + path}])).open_download(CANONICAL, "cover")
    try:
        assert download.length == len(media[key])
        assert read_all(download) == media[key]  # including the first bytes that were read to tell what it is
        assert (download.media_type, download.filename) == (media_type, f"tiktok_scout2015_6718335390845095173_cover.{ext}")
    finally:
        download.close()


def test_an_unlabelled_file_that_is_not_a_raster_image_is_never_relayed(cdn: Cdn, media: dict[str, bytes]) -> None:
    result = tiktok_like(cdn, thumbnails=[
        {"id": "a", "url": cdn.url + "/icon-unlabelled.svg", "preference": 3},
        {"id": "b", "url": cdn.url + "/page-unlabelled.html", "preference": 2},
        {"id": "c", "url": cdn.url + "/empty-unlabelled", "preference": 1},
        {"id": "d", "url": cdn.url + "/cover-unlabelled.jpg", "preference": 0},
    ])  # fmt: skip
    download = make_backend(result).open_download(CANONICAL, "cover")
    try:
        assert read_all(download) == media["cover.jpg"]
    finally:
        download.close()
    assert cdn.fetched() == ["/icon-unlabelled.svg", "/page-unlabelled.html", "/empty-unlabelled", "/cover-unlabelled.jpg"]


def test_a_post_whose_only_cover_is_unlabelled_script_has_no_cover(cdn: Cdn) -> None:
    result = tiktok_like(cdn, thumbnails=[{"id": "a", "url": cdn.url + "/icon-unlabelled.svg"}])
    with pytest.raises(FormatUnavailable):
        make_backend(result).open_download(CANONICAL, "cover")


def test_the_response_is_registered_before_the_post_session_that_owns_it(cdn: Cdn) -> None:
    """Downloads close cleanups in registration order, so this makes the response close first."""
    download = make_backend(tiktok_like(cdn)).open_download(CANONICAL, "video")
    try:
        owners = [type(getattr(fn, "__self__", None)).__name__ for fn in download._cleanups]
        assert owners[-1] == "PostSession" and "PostSession" not in owners[:-1]
        assert len(owners) == 2  # the response, then the session
    finally:
        download.close()


def test_a_post_without_a_usable_cover_says_so(cdn: Cdn) -> None:
    result = tiktok_like(cdn, thumbnails=[{"id": "a", "url": cdn.url + "/page.html"}])
    with pytest.raises(FormatUnavailable):
        make_backend(result).open_download(CANONICAL, "cover")


# ---------------------------------------------------------------------- sweeping


def test_sweep_removes_only_old_snaptik_directories(tmp_path: Path) -> None:
    old, fresh, other, plain_file = (tmp_path / n for n in ("snaptik-old", "snaptik-fresh", "unrelated", "snaptik-file"))
    for folder in (old, fresh, other):
        folder.mkdir()
        (folder / "f").write_text("x")
    plain_file.write_text("x")
    two_hours_ago = time.time() - 7200
    for path in (old, other, plain_file):
        os.utime(path, (two_hours_ago, two_hours_ago))
    sweep_stale_tempdirs(str(tmp_path), max_age=3600)
    assert not old.exists()
    assert fresh.exists() and other.exists() and plain_file.exists()
