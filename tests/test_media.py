"""Format selection and option building: the logic most likely to need updating when TikTok changes."""
from __future__ import annotations

import json

import pytest

from app.media import (
    KIND_AUDIO,
    KIND_COVER,
    KIND_META,
    KIND_VIDEO,
    KIND_VIDEO_SD,
    KIND_VIDEO_WM,
    audio_plan,
    build_options,
    download_stem,
    format_size,
    hashtags,
    is_slideshow,
    parse_info,
    parse_profile_entry,
    post_kind,
    post_record,
    posted_at,
    posted_date,
    safe_filename,
    video_candidates,
)
from tests.fakes import CANONICAL, fmt, make_info, sample_formats


def ids(formats) -> list[str]:
    return [f.format_id for f in formats]


# ------------------------------------------------------------------ video choice


def test_best_video_is_the_highest_resolution_h264_without_watermark() -> None:
    info = make_info()
    assert ids(video_candidates(info, KIND_VIDEO))[0] == "h264_1080p_2500000"


def test_unplayable_watermarked_and_video_only_formats_are_never_offered_as_the_main_video() -> None:
    info = make_info(formats=[
        *sample_formats(),
        fmt("hvc1_video_only", w=1080, h=1920, vcodec="h265", acodec="none", tbr=9999),
    ])
    candidates = ids(video_candidates(info, KIND_VIDEO))
    assert "bytevc2_1080p_1000000" not in candidates  # UNPLAYABLE
    assert "download" not in candidates  # watermarked
    assert "hvc1_video_only" not in candidates  # no audio track
    assert "audio" not in candidates


def test_h264_is_preferred_over_h265_at_equal_resolution() -> None:
    info = make_info(formats=[
        fmt("hevc", w=1080, h=1920, vcodec="h265", tbr=5000),
        fmt("avc", w=1080, h=1920, vcodec="h264", tbr=2000),
    ])
    assert ids(video_candidates(info, KIND_VIDEO)) == ["avc", "hevc"]


def test_higher_resolution_wins_even_when_only_h265_has_it_and_sd_offers_the_h264() -> None:
    info = make_info(formats=[
        fmt("hevc_1080", w=1080, h=1920, vcodec="h265", tbr=3000),
        fmt("avc_720", w=720, h=1280, vcodec="h264", tbr=1500),
        fmt("hevc_720", w=720, h=1280, vcodec="h265", tbr=900),
    ])
    assert video_candidates(info, KIND_VIDEO)[0].format_id == "hevc_1080"
    assert video_candidates(info, KIND_VIDEO_SD)[0].format_id == "avc_720"


def test_sd_is_the_next_resolution_down_and_absent_when_there_is_only_one() -> None:
    assert ids(video_candidates(make_info(), KIND_VIDEO_SD))[0] == "h264_540p_900000"
    only_one = make_info(formats=[fmt("only", w=576, h=1024)])
    assert video_candidates(only_one, KIND_VIDEO_SD) == []


def test_watermarked_copy_is_offered_separately_only_when_a_clean_one_exists() -> None:
    assert ids(video_candidates(make_info(), KIND_VIDEO_WM)) == ["download"]
    marked_only = make_info(formats=[fmt("download", w=576, h=1024, note="watermarked")])
    assert ids(video_candidates(marked_only, KIND_VIDEO)) == ["download"]  # better than nothing
    assert video_candidates(marked_only, KIND_VIDEO_WM) == []  # not offered twice


def test_watermark_is_detected_from_the_note_even_with_an_unfamiliar_format_id() -> None:
    info = make_info(formats=[
        fmt("download_addr", w=576, h=1024, note="Download video, watermarked"),
        fmt("play_addr", w=576, h=1024, note="Direct video"),
    ])
    assert ids(video_candidates(info, KIND_VIDEO)) == ["play_addr"]
    assert ids(video_candidates(info, KIND_VIDEO_WM)) == ["download_addr"]


def test_duplicate_ids_renamed_by_yt_dlp_do_not_hide_the_watermark() -> None:
    info = make_info(formats=[
        fmt("download-0", w=576, h=1024),
        fmt("download-1", w=576, h=1024),
        fmt("h264_540p_900000-0", w=576, h=1024, tbr=900),
    ])
    assert ids(video_candidates(info, KIND_VIDEO)) == ["h264_540p_900000-0"]
    assert ids(video_candidates(info, KIND_VIDEO_WM)) == ["download-0", "download-1"]


def test_resolution_is_the_short_side_so_landscape_and_portrait_agree() -> None:
    assert fmt("p", w=1080, h=1920).resolution == 1080
    assert fmt("l", w=1920, h=1080).resolution == 1080
    assert fmt("unknown").resolution is None


def test_unknown_kinds_have_no_candidates() -> None:
    assert video_candidates(make_info(), "nonsense") == []


# ------------------------------------------------------------------------- audio


def test_native_mp3_is_streamed_as_is() -> None:
    info = make_info(formats=[*sample_formats(), fmt("audio", vcodec="none", ext="mp3")])
    info.formats = [f for f in info.formats if not (f.format_id == "audio" and f.ext == "m4a")]
    plan = audio_plan(info, ffmpeg_available=True)
    assert plan and (plan.ext, plan.transcode, plan.from_video) == ("mp3", False, False)


def test_native_m4a_is_converted_to_mp3_when_ffmpeg_exists() -> None:
    plan = audio_plan(make_info(), ffmpeg_available=True)
    assert plan and (plan.ext, plan.transcode, plan.from_video) == ("mp3", True, False)
    assert plan.source.ext == "m4a"


def test_native_m4a_is_streamed_as_m4a_without_ffmpeg() -> None:
    plan = audio_plan(make_info(), ffmpeg_available=False)
    assert plan and (plan.ext, plan.transcode) == ("m4a", False)


def test_without_a_native_track_the_audio_comes_from_the_smallest_video() -> None:
    info = make_info(formats=[f for f in sample_formats() if f.format_id != "audio"])
    plan = audio_plan(info, ffmpeg_available=True)
    assert plan and plan.from_video and plan.transcode
    # Cheapest to fetch: lowest resolution, then lowest known bitrate (unknown bitrate counts as large).
    assert plan.source.format_id == "h264_540p_900000"
    assert audio_plan(info, ffmpeg_available=False) is None


def test_no_media_at_all_means_no_audio() -> None:
    assert audio_plan(make_info(formats=[]), ffmpeg_available=True) is None


# ----------------------------------------------------------------------- options


def test_options_for_a_typical_post() -> None:
    options = build_options(make_info(), ffmpeg_available=True)
    assert [o.kind for o in options] == [KIND_VIDEO, KIND_VIDEO_SD, KIND_AUDIO, KIND_COVER, KIND_VIDEO_WM, KIND_META]
    assert [o.primary for o in options] == [True, False, False, False, False, False]

    main, sd, audio, cover, marked, details = options
    assert (main.title, main.detail, main.tag) == ("Video · 1080p", "MP4 · H.264 · 6.6 MB", "No watermark")
    assert sd.title == "Video · 576p"
    assert (audio.title, audio.detail, audio.ext) == ("Audio · MP3", "original sound - scout2015", "mp3")
    assert cover.title == "Cover image"
    assert marked.tag == "Watermarked"
    assert (details.title, details.ext) == ("Caption & details", "json")


def test_audio_is_labelled_m4a_when_it_cannot_be_converted() -> None:
    audio = next(o for o in build_options(make_info(), ffmpeg_available=False) if o.kind == KIND_AUDIO)
    assert (audio.title, audio.ext) == ("Audio · M4A", "m4a")


def test_size_is_estimated_from_bitrate_when_unknown() -> None:
    info = make_info(duration=10, formats=[fmt("x", w=576, h=1024, tbr=800)])  # 800 kbit/s * 10 s = 1,000,000 bytes
    assert build_options(info, ffmpeg_available=False)[0].detail == "MP4 · H.264 · ~977 KB"


def test_slideshow_post_offers_sound_and_cover_only() -> None:
    info = make_info(formats=[f for f in sample_formats() if f.audio_only])
    assert is_slideshow(info)
    options = build_options(info, ffmpeg_available=True)
    assert [o.kind for o in options] == [KIND_AUDIO, KIND_COVER, KIND_META]
    assert options[0].primary  # the sound is the main thing you can get
    assert not is_slideshow(make_info())


def test_a_post_with_nothing_downloadable_has_no_options() -> None:
    assert build_options(make_info(formats=[], covers=[], thumbnail=None), ffmpeg_available=True) == []


@pytest.mark.parametrize(("size", "text"), [(500, "1 KB"), (150_000, "146 KB"), (1_048_576, "1.0 MB"), (6_900_000, "6.6 MB")])
def test_format_size(size: int, text: str) -> None:
    assert format_size(size) == text


# ----------------------------------------------------------- yt-dlp output parsing


def raw_info() -> dict:
    """Shaped like what YoutubeDL.extract_info() returns for TikTok's web extractor."""
    return {
        "id": "6718335390845095173",
        "title": "  cute\ncat  video ",
        "description": "cute cat video #cats",
        "channel": "Scout",
        "uploader": "scout2015",
        "duration": 15,
        "view_count": 100,
        "like_count": 10,
        "comment_count": None,
        "repost_count": True,  # bools are not numbers
        "track": "original sound",
        "artists": ["Scout", "", 7],
        "thumbnail": "https://cdn.example.test/best.jpg",
        "thumbnails": [
            {"id": "dynamicCover", "url": "https://cdn.example.test/dyn.webp", "preference": -2},
            {"id": "cover", "url": "https://cdn.example.test/cover.jpg", "preference": -1},
            {"id": "bad", "url": "javascript:alert(1)", "preference": 5},
        ],
        "formats": [
            {"format_id": "h264_540p_900000", "url": "https://cdn.example.test/a.mp4", "ext": "mp4", "vcodec": "h264",
             "acodec": "aac", "width": 576, "height": 1024, "tbr": 900.0, "filesize": 2_400_000, "protocol": "https",
             "http_headers": {"Referer": "https://www.tiktok.com/", "Cookie": "sid=secret", "Accept-Encoding": "gzip",
                              "User-Agent": "UA"}},
            {"format_id": "hls", "url": "https://cdn.example.test/a.m3u8", "protocol": "m3u8_native", "vcodec": "h264"},
            {"format_id": "ftp", "url": "ftp://cdn.example.test/a.mp4"},
            {"format_id": "nourl"},
            "garbage",
            {"format_id": "audio", "url": "https://cdn.example.test/s.m4a", "ext": "m4a", "vcodec": "none"},
        ],
    }


def test_parse_info_cleans_and_filters_yt_dlp_output() -> None:
    info = parse_info(raw_info(), CANONICAL)
    assert info.id == "6718335390845095173"
    assert info.title == "cute cat video"  # whitespace collapsed
    assert (info.author_name, info.author_handle) == ("Scout", "scout2015")
    assert info.stats == {"views": 100, "likes": 10, "comments": None, "shares": None}
    assert info.artists == ["Scout"]
    # static covers come first; the animated one is the fallback; non-http URLs are dropped
    assert info.covers == [
        "https://cdn.example.test/cover.jpg",
        "https://cdn.example.test/dyn.webp",
        "https://cdn.example.test/best.jpg",
    ]
    assert info.thumbnail == "https://cdn.example.test/cover.jpg"
    assert ids(info.formats) == ["h264_540p_900000", "audio"]  # no HLS, no ftp, no url-less entries


def test_parse_info_drops_headers_that_must_not_be_replayed() -> None:
    headers = parse_info(raw_info(), CANONICAL).formats[0].headers
    assert headers == {"Referer": "https://www.tiktok.com/", "User-Agent": "UA"}


def test_parse_info_survives_an_almost_empty_result() -> None:
    info = parse_info({}, CANONICAL)
    assert info.id == "6718335390845095173"  # recovered from the URL
    assert info.formats == [] and info.covers == [] and info.thumbnail is None


def test_handles_with_odd_characters_are_not_used_in_filenames() -> None:
    assert parse_info({"uploader": "../../etc/passwd"}, CANONICAL).author_handle is None


# --------------------------------------------------------------------- filenames


@pytest.mark.parametrize(
    ("stem", "ext", "expected"),
    [
        ("tiktok_scout_123", "mp4", "tiktok_scout_123.mp4"),
        ("../../etc/passwd", "mp4", "etc_passwd.mp4"),
        ('evil"\r\nSet-Cookie: x=1', "mp4", "evil_Set-Cookie_x_1.mp4"),
        ("名前 emoji 😀", "mp3", "emoji.mp3"),
        ("", "mp4", "tiktok.mp4"),
        ("...", "mp4", "tiktok.mp4"),
        ("a" * 200, "mp4", "a" * 80 + ".mp4"),
        ("x", "MP4", "x.mp4"),
        ("x", "../exe", "x.exe"),
        ("x", "", "x.bin"),
    ],
)
def test_safe_filename_is_ascii_and_header_safe(stem: str, ext: str, expected: str) -> None:
    assert safe_filename(stem, ext) == expected


def test_download_stem_skips_missing_parts() -> None:
    assert download_stem(make_info(), "cover") == "tiktok_scout2015_6718335390845095173_cover"
    assert download_stem(make_info(author_handle=None)) == "tiktok_6718335390845095173"


# ------------------------------------------------------------- post details (captions)


def test_hashtags_are_unique_ordered_and_unicode_aware() -> None:
    assert hashtags("so #Cute and #cute again #猫 #fyp #fyp") == ["Cute", "cute", "猫", "fyp"]
    assert hashtags("no tags here, just a # and 5#") == ["5"] or hashtags("no tags here, just a # and 5#") == []


@pytest.mark.parametrize(
    ("timestamp", "iso", "day"),
    [
        (1_561_572_000, "2019-06-26T18:00:00Z", "2019-06-26"),
        (0, "1970-01-01T00:00:00Z", "1970-01-01"),
        (None, None, None),
        (10**15, None, None),  # beyond what a date can hold: dropped rather than crashing
        (-(10**15), None, None),
    ],
)
def test_post_dates_tolerate_missing_and_absurd_timestamps(timestamp: int | None, iso: str | None, day: str | None) -> None:
    info = make_info(timestamp=timestamp)
    assert (posted_at(info), posted_date(info)) == (iso, day)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1_561_572_000, 1_561_572_000),
        (1_561_572_000.9, 1_561_572_000),
        (1_561_572_000_000, None),  # milliseconds, not seconds: no real post is dated in the year 51000
        (0, None),
        (-5, None),
        (315_532_799, None),  # just before 1980, which an archive cannot store
        (4_354_819_200, None),  # the first second after 2107, likewise
        (float("nan"), None),
        (float("inf"), None),
        ("1561572000", None),
        (True, None),
        (None, None),
    ],
)
def test_a_publication_date_that_cannot_be_right_is_dropped_at_parsing(raw: object, expected: int | None) -> None:
    """One garbage date must never be able to break an archive part-way through: it just reads as 'undated'."""
    assert parse_info({"timestamp": raw}, CANONICAL).timestamp == expected
    entry = {"id": "7123456789012345678", "timestamp": raw}
    post = parse_profile_entry(entry, "scout2015")
    assert post is not None and post.timestamp == expected


def test_the_post_record_has_everything_worth_keeping() -> None:
    info = make_info(timestamp=1_561_572_000, description="Hello #fyp #cats second line",
                     caption="Hello #fyp #cats\nsecond line")  # fmt: skip
    record = post_record(info)
    assert record == {
        "id": "6718335390845095173",
        "url": CANONICAL,
        "type": "video",
        "caption": "Hello #fyp #cats\nsecond line",  # the archived caption keeps its line breaks
        "hashtags": ["fyp", "cats"],
        "author": {"name": "Scout", "handle": "scout2015"},
        "posted_at": "2019-06-26T18:00:00Z",
        "duration": 15,
        "stats": {"views": 1_234_567, "likes": 84_200, "comments": 1_290, "shares": 3_410},
        "sound": {"title": "original sound - scout2015", "artists": ["Scout"]},
        "photo_count": None,
    }
    json.dumps(record)  # must be serialisable as is


def test_post_kind_distinguishes_video_photos_and_sound_only() -> None:
    assert post_kind(make_info()) == "video"
    assert post_kind(make_info(images=[["https://cdn.example.test/1.jpg"]])) == "photos"
    assert post_kind(make_info(formats=[f for f in sample_formats() if f.audio_only])) == "audio"


def test_photo_urls_from_yt_dlp_info_are_sanitised() -> None:
    raw = {"snaptik_images": [["https://a.test/1.jpg", "javascript:alert(1)", "https://a.test/1.jpg"], [], "x", None,
                              ["ftp://a.test/2.jpg"], ["http://b.test/3.png"]]}  # fmt: skip
    assert parse_info(raw, CANONICAL).images == [["https://a.test/1.jpg"], ["http://b.test/3.png"]]
    assert parse_info({"snaptik_images": "not a list"}, CANONICAL).images == []


def test_photo_posts_lead_with_the_photos_and_keep_sound_secondary() -> None:
    info = make_info(images=[["https://a.test/1.jpg"], ["https://a.test/2.jpg"]],
                     formats=[f for f in sample_formats() if f.audio_only])  # fmt: skip
    options = build_options(info, ffmpeg_available=True)
    assert [o.kind for o in options] == ["images", "audio", "cover", "meta"]
    assert [o.primary for o in options] == [True, False, False, False]
    assert (options[0].title, options[0].ext) == ("Photos · 2", "zip")
    assert is_slideshow(info)


def test_the_archived_caption_keeps_line_breaks_but_the_display_text_is_one_line() -> None:
    raw = {"description": "line one\r\nline two  \n\n\x00line\x07 four\t!"}
    info = parse_info(raw, CANONICAL)
    assert info.caption == "line one\nline two\n\nline four\t!"
    assert info.description == "line one line two line four !"
    assert parse_info({"description": "x" * 5000}, CANONICAL).caption.endswith("…")
    assert parse_info({}, CANONICAL).caption == ""
