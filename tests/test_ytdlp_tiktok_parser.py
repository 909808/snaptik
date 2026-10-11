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

import io
import json
import zipfile
from typing import Any

import pytest
from yt_dlp.extractor.tiktok import TikTokIE

from app.backend import TikTokPhotosIE, YtDlpBackend
from app.config import Settings
from app.errors import FormatUnavailable
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
    page_loads: list[str] = []

    class TikTokPageFixtureIE(TikTokPhotosIE):
        def _extract_web_data_and_status(self, url: str, video_id: str, fatal: bool = True):  # type: ignore[override]
            page_loads.append(url)
            return blob, 0

    class Backend(YtDlpBackend):
        IE_KEY = TikTokPageFixtureIE.ie_key()

        def _new_ydl(self, **overrides):  # type: ignore[no-untyped-def]
            ydl = super()._new_ydl(**overrides)
            ydl.add_info_extractor(TikTokPageFixtureIE())
            return ydl

    backend = Backend(Settings(allow_private_upstream=True, network_timeout=5, **settings))
    backend.page_loads = page_loads  # type: ignore[attr-defined]
    return backend


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
    assert list(options) == ["video", "video_sd", "audio", "cover", "video_wm", "meta"]
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
    assert [o.kind for o in build_options(info, ffmpeg_available=True)] == ["audio", "cover", "meta"]
    assert info.duration == 15  # yt-dlp falls back to the sound's length


# ------------------------------------------------------------ photo posts and details


def photo_blob(cdn: Cdn, **overrides: Any) -> dict[str, Any]:
    """A photo post: no playable video, but ``imagePost.images`` (read by our extractor subclass)."""

    def photo(*paths: str) -> dict[str, Any]:
        return {"imageURL": {"urlList": [f"{cdn.url}{p}" for p in paths]}, "imageWidth": 64, "imageHeight": 96}

    blob = page_blob(cdn)
    blob["video"] = {"duration": 0, "cover": f"{cdn.url}/cover.jpg"}
    blob["imagePost"] = {"images": [photo("/dead.mp4", "/photo1.jpg"), photo("/photo2.jpg"), photo("/photo3.png")]}
    blob.update(overrides)
    return blob


def test_photos_survive_yt_dlps_parser_through_our_extractor(cdn: Cdn) -> None:
    info = backend_for(photo_blob(cdn)).fetch_info(CANONICAL)
    assert [[u.replace(cdn.url, "") for u in urls] for urls in info.images] == [
        ["/dead.mp4", "/photo1.jpg"],
        ["/photo2.jpg"],
        ["/photo3.png"],
    ]
    assert is_slideshow(info)
    options = build_options(info, ffmpeg_available=True)
    assert [o.kind for o in options] == ["images", "audio", "cover", "meta"]
    assert (options[0].title, options[0].primary, options[1].primary) == ("Photos · 3", True, False)


def test_a_normal_video_post_has_no_photos(cdn: Cdn) -> None:
    assert backend_for(page_blob(cdn)).fetch_info(CANONICAL).images == []


def test_photos_download_as_a_valid_zip_with_the_caption(cdn: Cdn, media: dict[str, bytes]) -> None:
    download = backend_for(photo_blob(cdn)).open_download(CANONICAL, "images")
    try:
        assert (download.media_type, download.filename) == ("application/zip", "tiktok_scout2015_6718335390845095173_photos.zip")
        data = read_all(download)
    finally:
        download.close()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == ["01.jpg", "02.jpg", "03.png", "details.json"]
        assert archive.read("01.jpg") == media["cover.jpg"]  # the first photo's dead CDN host was skipped
        assert archive.read("02.jpg") == media["photo2.jpg"]
        assert archive.read("03.png") == media["photo3.png"]
        details = json.loads(archive.read("details.json"))
    assert (details["type"], details["photo_count"], details["author"]["handle"]) == ("photos", 3, "scout2015")


def test_a_photo_that_cannot_be_fetched_is_left_out_and_listed(cdn: Cdn, media: dict[str, bytes]) -> None:
    blob = photo_blob(cdn)
    blob["imagePost"]["images"][1] = {"imageURL": {"urlList": [f"{cdn.url}/dead.mp4"]}}
    download = backend_for(blob).open_download(CANONICAL, "images")
    try:
        data = read_all(download)
    finally:
        download.close()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == ["01.jpg", "03.png", "details.json", "SKIPPED.txt"]
        assert archive.read("SKIPPED.txt") == b"These photos could not be downloaded: 2\n"


def test_if_the_first_photo_is_unreachable_the_download_fails_before_any_bytes(cdn: Cdn) -> None:
    blob = photo_blob(cdn)
    blob["imagePost"]["images"][0] = {"imageURL": {"urlList": [f"{cdn.url}/dead.mp4"]}}
    with pytest.raises(FormatUnavailable):
        backend_for(blob).open_download(CANONICAL, "images")


def test_a_post_without_photos_has_no_photo_download(cdn: Cdn) -> None:
    with pytest.raises(FormatUnavailable):
        backend_for(page_blob(cdn)).open_download(CANONICAL, "images")


def test_caption_and_details_come_as_json(cdn: Cdn) -> None:
    download = backend_for(page_blob(cdn)).open_download(CANONICAL, "meta")
    try:
        assert (download.media_type, download.filename) == ("application/json", "tiktok_scout2015_6718335390845095173_details.json")
        details = json.loads(read_all(download))
    finally:
        download.close()
    assert details["caption"].startswith("cute cat video #cats")
    assert details["hashtags"] == ["cats"]
    assert details["posted_at"] == "2019-06-26T18:00:00Z"  # createTime 1561572000
    assert details["stats"] == {"views": 1234, "likes": 56, "comments": 8, "shares": 7}
    assert details["sound"] == {"title": "original sound - scout2015", "artists": ["Scout"]}
    assert (details["type"], details["author"]) == ("video", {"name": "Scout", "handle": "scout2015"})


def test_one_lookup_serves_every_file_of_a_post(cdn: Cdn) -> None:
    """Saving a whole profile must cost one TikTok page load per post, not one per file."""
    backend = backend_for(page_blob(cdn))
    with backend.open_post(CANONICAL) as post:
        for kind in ("video", "audio", "cover", "meta"):
            download = post.open(kind)
            try:
                assert read_all(download)
            finally:
                download.close()
    assert len(backend.page_loads) == 1  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        # TikTok's web data
        ({"imagePost": {"images": [{"imageURL": {"urlList": ["https://a.test/1.jpg", "https://b.test/1.jpg"]}}]}},
         [["https://a.test/1.jpg", "https://b.test/1.jpg"]]),
        # the mobile API's spelling
        ({"image_post_info": {"images": [{"display_image": {"url_list": ["https://a.test/1.webp"]}}]}},
         [["https://a.test/1.webp"]]),
        # both holders on one photo: the original-size one comes first, duplicates are dropped
        ({"imagePost": {"images": [{"displayImage": {"urlList": ["https://a.test/small.jpg"]},
                                    "imageURL": {"urlList": ["https://a.test/big.jpg", "https://a.test/small.jpg"]}}]}},
         [["https://a.test/big.jpg", "https://a.test/small.jpg"]]),
        # junk is ignored, good photos around it are kept
        ({"imagePost": {"images": [None, "x", {}, {"imageURL": {"urlList": ["javascript:x", 5]}},
                                   {"imageURL": {"urlList": ["https://a.test/ok.jpg"]}}]}}, [["https://a.test/ok.jpg"]]),
        ({"imagePost": {"images": "nope"}}, []),
        ({"imagePost": None}, []),
        ({}, []),
        ("not a mapping", []),
    ],
)  # fmt: skip
def test_photo_data_shapes(data: Any, expected: list[list[str]]) -> None:
    from app.backend import _extract_images

    assert _extract_images(data) == expected
