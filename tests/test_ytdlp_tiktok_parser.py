"""Canary: yt-dlp's real TikTok parsing code, fed a TikTok-shaped page, then run through our code.

Only the network step is stubbed: ``TikTokIE._extract_web_data_and_status`` normally downloads
tiktok.com and returns the JSON blob embedded in the page; here it returns a fixture built from
the keys the extractor reads (``video.bitrateInfo[].PlayAddr``, ``video.playAddr``,
``video.downloadAddr``, ``music.playUrl``, ``stats``, ...). Everything after that is the real
thing: ``_parse_aweme_video_web`` / ``_extract_web_formats``, yt-dlp's format processing, and
our selection logic.

If this fails after a yt-dlp upgrade, TikTok's format naming or yt-dlp's internals changed:
re-check app/media.py against yt_dlp/extractor/tiktok.py. It cannot tell you whether TikTok's
live site still matches the fixture; only a request to tiktok.com can.
"""
from __future__ import annotations

from typing import Any

import pytest
from yt_dlp.extractor.tiktok import TikTokIE

from app.backend import YtDlpBackend
from app.config import Settings
from app.media import build_options, is_slideshow, video_candidates
from tests.fakes import CANONICAL
from tests.helpers import Cdn, needs_ffmpeg, read_all

pytestmark = pytest.mark.skipif(
    not hasattr(TikTokIE, "_extract_web_data_and_status"),
    reason="yt-dlp's TikTok extractor no longer has the hook this canary stubs; update the test",
)


def page_blob(cdn: Cdn, **overrides: Any) -> dict[str, Any]:
    def address(path: str, key: str, size: int, width: int, height: int) -> dict[str, Any]:
        # TikTok lists several CDN hosts per rendition and yt-dlp turns each into a format (it also
        # drops formats with a repeated URL, so every URL must be unique). The first host is dead.
        return {"Uri": key, "UrlList": [f"{cdn.url}/dead.mp4?k={key}", f"{cdn.url}{path}?k={key}"],
                "UrlKey": key, "DataSize": str(size), "Width": width, "Height": height}  # fmt: skip

    blob = {
        "id": "6718335390845095173",
        "desc": "cute cat video #cats " + "very long caption " * 10,
        "createTime": "1561572000",
        "author": {"id": "6666", "uniqueId": "scout2015", "nickname": "Scout", "secUid": "MS4wLjABAAAAx"},
        "music": {
            "title": "original sound - scout2015",
            "authorName": "Scout",
            "duration": 15,
            "playUrl": f"{cdn.url}/sound.mp3?mime_type=audio_mpeg",
        },
        "stats": {"playCount": 1234, "diggCount": 56, "shareCount": 7, "commentCount": 8, "collectCount": 9},
        "video": {
            "width": 576,
            "height": 1024,
            "duration": 15,
            "cover": f"{cdn.url}/page.html?small",
            "originCover": f"{cdn.url}/cover.jpg",
            "dynamicCover": f"{cdn.url}/page.html?animated",
            "playAddr": f"{cdn.url}/video-sd.mp4?play=1",
            "downloadAddr": f"{cdn.url}/video-wm.mp4",
            "bitrateInfo": [
                {"PlayAddr": address("/video-sd.mp4", "v09044g_h264_540p_900000", 2_400_000, 576, 1024)},
                {"PlayAddr": address("/video.mp4", "v09044g_h264_1080p_2500000", 6_900_000, 1080, 1920)},
                {"PlayAddr": address("/video.mp4", "v09044g_bytevc1_1080p_1600000", 4_300_000, 1080, 1920)},
                {"PlayAddr": address("/video.mp4", "v09044g_bytevc2_1080p_1000000", 2_800_000, 1080, 1920)},
            ],
        },
    }
    blob.update(overrides)
    return blob


def backend_for(blob: dict[str, Any], **settings: Any) -> YtDlpBackend:
    class TikTokPageFixtureIE(TikTokIE):
        def _extract_web_data_and_status(self, url: str, video_id: str, fatal: bool = True):  # type: ignore[override]
            return blob, 0

    class Backend(YtDlpBackend):
        IE_KEY = TikTokPageFixtureIE.ie_key()

        def _new_ydl(self):  # type: ignore[no-untyped-def]
            ydl = super()._new_ydl()
            ydl.add_info_extractor(TikTokPageFixtureIE())
            return ydl

    return Backend(Settings(allow_private_upstream=True, network_timeout=5, **settings))


def test_metadata_survives_yt_dlps_real_parser(cdn: Cdn) -> None:
    info = backend_for(page_blob(cdn)).fetch_info(CANONICAL)
    assert (info.id, info.author_handle, info.author_name) == ("6718335390845095173", "scout2015", "Scout")
    assert info.duration == 15
    assert info.stats == {"views": 1234, "likes": 56, "comments": 8, "shares": 7}
    assert (info.track, info.artists) == ("original sound - scout2015", ["Scout"])
    assert info.title.startswith("cute cat video #cats") and len(info.title) < len(info.description)
    # Static covers first (the original-size one outranks the small one), the animated one last.
    assert [c.rsplit("/", 1)[1] for c in info.covers] == ["cover.jpg", "page.html?small", "page.html?animated"]


def test_options_match_what_a_user_should_see(cdn: Cdn) -> None:
    info = backend_for(page_blob(cdn)).fetch_info(CANONICAL)

    main = video_candidates(info, "video")
    assert (main[0].resolution, main[0].codec_name, main[0].watermarked, main[0].filesize) == (1080, "H.264", False, 6_900_000)
    assert not any("bytevc2" in f.format_id for f in info.formats if not f.unplayable)
    assert all(not f.watermarked and not f.unplayable for f in main)
    assert [f.codec_name for f in main if f.resolution == 1080] == ["H.264", "H.264", "H.265", "H.265"]  # two CDN hosts each

    assert video_candidates(info, "video_sd")[0].resolution == 576
    assert all(f.watermarked for f in video_candidates(info, "video_wm"))

    options = {o.kind: o for o in build_options(info, ffmpeg_available=True)}
    assert list(options) == ["video", "video_sd", "audio", "cover", "video_wm"]
    assert options["video"].title == "Video · 1080p" and options["video"].detail == "MP4 · H.264 · 6.6 MB"
    assert options["audio"].ext == "mp3"  # the sound's own mp3 is delivered as is, no conversion needed
    assert options["video_wm"].tag == "Watermarked"


def test_a_dead_cdn_host_falls_back_to_the_next_host_of_the_same_rendition(cdn: Cdn, media: dict[str, bytes]) -> None:
    download = backend_for(page_blob(cdn)).open_download(CANONICAL, "video")
    try:
        assert read_all(download) == media["video.mp4"]
    finally:
        download.close()
    assert cdn.fetched() == ["/dead.mp4", "/video.mp4"]
    assert all("h264_1080p_2500000" in r["query"] for r in cdn.requests)  # still 1080p: no quality was lost


def test_all_kinds_download_through_the_real_pipeline(cdn: Cdn, media: dict[str, bytes]) -> None:
    backend = backend_for(page_blob(cdn))
    expected = {
        "video_wm": media["video.mp4"] + b"\x00wm",
        "audio": media["sound.mp3"],
        "cover": media["cover.jpg"],
    }
    for kind, body in expected.items():
        download = backend.open_download(CANONICAL, kind)
        try:
            assert read_all(download) == body, kind
        finally:
            download.close()


@needs_ffmpeg
def test_an_m4a_sound_track_is_converted(cdn: Cdn, tmp_path: Any) -> None:
    blob = page_blob(cdn)
    blob["music"]["playUrl"] = f"{cdn.url}/sound.m4a"  # no mime_type hint: yt-dlp falls back to m4a
    backend = backend_for(blob, temp_dir=str(tmp_path))
    info = backend.fetch_info(CANONICAL)
    audio = next(o for o in build_options(info, ffmpeg_available=True) if o.kind == "audio")
    assert (audio.title, audio.ext) == ("Audio · MP3", "mp3")

    download = backend.open_download(CANONICAL, "audio")
    try:
        assert read_all(download)[:3] == b"ID3"  # an MP3 with an ID3 tag
        assert download.media_type == "audio/mpeg"
    finally:
        download.close()


def test_photo_slideshows_come_through_as_sound_and_cover_only(cdn: Cdn) -> None:
    blob = page_blob(cdn)
    blob["video"] = {"duration": 0, "cover": f"{cdn.url}/cover.jpg"}  # no playable video, as for photo posts
    info = backend_for(blob).fetch_info(CANONICAL)
    assert is_slideshow(info)
    assert [o.kind for o in build_options(info, ffmpeg_available=True)] == ["audio", "cover"]
    assert info.duration == 15  # yt-dlp falls back to the sound's length
