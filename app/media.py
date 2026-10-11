"""The small typed model the rest of the app uses, built from yt-dlp's output.

Everything here is pure (no network, no yt-dlp import), so it is easy to test and
the one place to look when TikTok changes what its formats look like.

What yt-dlp's TikTok extractor gives us (see ``yt_dlp/extractor/tiktok.py``):

* video formats named like ``h264_540p_...`` / ``play`` / ``play_addr``: no watermark;
* a ``download`` / ``download_addr`` format whose note says "watermarked";
* ``bytevc2`` formats flagged UNPLAYABLE (ByteDance's own codec): never offered;
* an ``audio`` format (``vcodec == "none"``): the post's sound track;
* slideshow posts have the audio format only. yt-dlp does not read their photos, so the
  backend adds them under ``snaptik_images`` (see ``TikTokPhotosIE`` in backend.py).
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

KIND_VIDEO = "video"
KIND_VIDEO_SD = "video_sd"
KIND_VIDEO_WM = "video_wm"
KIND_VIDEO_H264 = "video_h264"  # the best H.264 file: plays on any device, even when TikTok has a sharper H.265 one
KIND_AUDIO = "audio"
KIND_COVER = "cover"
KIND_IMAGES = "images"  # all photos of a slideshow post, as a ZIP
KIND_META = "meta"  # caption and details, as JSON
VIDEO_KINDS = (KIND_VIDEO, KIND_VIDEO_SD, KIND_VIDEO_WM, KIND_VIDEO_H264)
DOWNLOAD_KINDS = (*VIDEO_KINDS, KIND_AUDIO, KIND_COVER, KIND_IMAGES, KIND_META)

_DROP_HEADERS = {"cookie", "host", "content-length", "accept-encoding"}


@dataclass(frozen=True, slots=True)
class MediaFormat:
    format_id: str
    url: str
    ext: str = "mp4"
    vcodec: str | None = None
    acodec: str | None = None
    width: int | None = None
    height: int | None = None
    tbr: float | None = None  # kbit/s
    filesize: int | None = None
    note: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    source_preference: int = 0

    @property
    def audio_only(self) -> bool:
        return self.vcodec == "none"

    @property
    def video_only(self) -> bool:
        return self.acodec == "none" and not self.audio_only

    @property
    def unplayable(self) -> bool:
        return "unplayable" in self.note.lower() or (self.vcodec or "") in {"bytevc2", "h266", "vvc"}

    @property
    def watermarked(self) -> bool:
        # yt-dlp renames duplicate ids ("download" -> "download-0"), so compare the base id.
        return "watermark" in self.note.lower() or self.format_id.split("-")[0] == "download"

    @property
    def resolution(self) -> int | None:
        """The short side in pixels, which is what "1080p" means for vertical video too."""
        sides = [s for s in (self.width, self.height) if s]
        return min(sides) if sides else None

    @property
    def codec_name(self) -> str:
        codec = self.vcodec or ""
        if codec.startswith(("h264", "avc")):
            return "H.264"
        if codec.startswith(("h265", "hevc", "hvc", "bytevc1")):
            return "H.265"
        return ""

    @property
    def codec_rank(self) -> int:
        """Prefer H.264 (plays everywhere) over unknown over H.265 at equal resolution."""
        return {"H.264": 2, "": 1, "H.265": 0}[self.codec_name]


@dataclass(slots=True)
class VideoInfo:
    id: str
    url: str
    title: str = ""
    description: str = ""
    author_name: str | None = None
    author_handle: str | None = None
    duration: int | None = None
    thumbnail: str | None = None
    covers: list[str] = field(default_factory=list)
    stats: dict[str, int | None] = field(default_factory=dict)
    track: str | None = None
    artists: list[str] = field(default_factory=list)
    formats: list[MediaFormat] = field(default_factory=list)
    timestamp: int | None = None  # when the post was published (unix seconds)
    images: list[list[str]] = field(default_factory=list)  # slideshow photos: candidate URLs for each
    caption: str = ""  # the full caption as written, line breaks kept (``description`` is one tidy line)


# --------------------------------------------------------------------------- parsing


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return int(value)
    except (ValueError, OverflowError):  # NaN and infinity
        return None


# What a ZIP entry can hold: 1980-01-01 to the end of 2107. A "publication date" outside that is garbage
# (milliseconds instead of seconds, say), and must not be allowed to break an archive part-way through.
TIMESTAMP_MIN, TIMESTAMP_MAX = 315_532_800, 4_354_819_199


def _timestamp(value: Any) -> int | None:
    stamp = _int(value)
    return stamp if stamp is not None and TIMESTAMP_MIN <= stamp <= TIMESTAMP_MAX else None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", value).split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"


def _multiline(value: Any, limit: int) -> str:
    """Text as written (line breaks kept) minus control characters, for what is archived."""
    if not isinstance(value, str):
        return ""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _is_http(value: Any) -> bool:
    return isinstance(value, str) and value.lower().startswith(("http://", "https://"))


def _codec(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) and value else None


def _handle(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", value):
        return value
    return None


def _parse_format(item: Any) -> MediaFormat | None:
    if not isinstance(item, Mapping) or not _is_http(item.get("url")):
        return None
    if str(item.get("protocol") or "https").lower() not in {"http", "https"}:
        return None  # HLS/DASH manifests: not a single downloadable file
    raw_headers = item.get("http_headers")
    headers = {
        str(k): str(v)
        for k, v in (raw_headers.items() if isinstance(raw_headers, Mapping) else ())
        if str(k).lower() not in _DROP_HEADERS
    }
    return MediaFormat(
        format_id=str(item.get("format_id") or ""),
        url=item["url"],
        ext=str(item.get("ext") or "mp4").lower(),
        vcodec=_codec(item.get("vcodec")),
        acodec=_codec(item.get("acodec")),
        width=_int(item.get("width")),
        height=_int(item.get("height")),
        tbr=_float(item.get("tbr")),
        filesize=_int(item.get("filesize")) or _int(item.get("filesize_approx")),
        note=str(item.get("format_note") or ""),
        headers=headers,
        source_preference=_int(item.get("source_preference")) or 0,
    )


def _cover_urls(raw: Mapping[str, Any]) -> list[str]:
    thumbs = [t for t in raw.get("thumbnails") or [] if isinstance(t, Mapping) and _is_http(t.get("url"))]
    # yt-dlp lists thumbnails worst to best, so reverse. TikTok gives the animated "dynamic
    # cover" a lower preference than the static covers, which keeps it last.
    urls = [t["url"] for t in reversed(thumbs)]
    if _is_http(raw.get("thumbnail")):
        urls.append(raw["thumbnail"])
    return list(dict.fromkeys(urls))


def _image_urls(raw: Any) -> list[list[str]]:
    """Per photo, its candidate URLs (TikTok lists several CDN hosts); anything malformed is dropped."""
    images = []
    for candidates in raw if isinstance(raw, list) else []:
        urls = [u for u in candidates if _is_http(u)] if isinstance(candidates, list) else []
        if urls:
            images.append(list(dict.fromkeys(urls)))
    return images


def parse_info(raw: Mapping[str, Any], canonical_url: str) -> VideoInfo:
    covers = _cover_urls(raw)
    artists = raw.get("artists")
    fallback_id = re.search(r"/video/(\d+)", canonical_url)
    return VideoInfo(
        id=str(raw.get("id") or (fallback_id.group(1) if fallback_id else "")),
        url=canonical_url,
        title=_text(raw.get("title"), 200),
        description=_text(raw.get("description"), 1000),
        author_name=_text(raw.get("channel") or raw.get("creator") or raw.get("uploader"), 100) or None,
        author_handle=_handle(raw.get("uploader")),
        duration=_int(raw.get("duration")),
        thumbnail=covers[0] if covers else None,
        covers=covers,
        stats={
            "views": _int(raw.get("view_count")),
            "likes": _int(raw.get("like_count")),
            "comments": _int(raw.get("comment_count")),
            "shares": _int(raw.get("repost_count")),
        },
        track=_text(raw.get("track"), 120) or None,
        artists=[_text(a, 80) for a in artists if isinstance(a, str) and a][:5] if isinstance(artists, list) else [],
        formats=[f for item in raw.get("formats") or [] if (f := _parse_format(item))],
        timestamp=_timestamp(raw.get("timestamp")),
        images=_image_urls(raw.get("snaptik_images")),
        caption=_multiline(raw.get("description"), 4000),
    )


# ------------------------------------------------------------------------- selection


def _video_pool(info: VideoInfo) -> list[MediaFormat]:
    return [f for f in info.formats if not f.audio_only and not f.video_only and not f.unplayable]


def _quality_key(fmt: MediaFormat) -> tuple[int, int, float, int]:
    return (fmt.resolution or 0, fmt.codec_rank, fmt.tbr or 0.0, fmt.source_preference)


def video_candidates(info: VideoInfo, kind: str) -> list[MediaFormat]:
    """Formats to try for ``kind``, best first. Later entries are fallbacks if one fails to open."""
    pool = _video_pool(info)
    clean = sorted((f for f in pool if not f.watermarked), key=_quality_key, reverse=True)
    marked = sorted((f for f in pool if f.watermarked), key=_quality_key, reverse=True)
    if kind == KIND_VIDEO:
        return clean or marked  # a watermarked file beats no file at all
    if kind == KIND_VIDEO_H264:
        # H.264 first, however sharp the H.265 alternatives are; those stay as fallbacks if no H.264 file opens.
        return [f for f in clean if f.codec_name == "H.264"] + [f for f in clean if f.codec_name != "H.264"] or marked
    if kind == KIND_VIDEO_WM:
        return marked if clean else []  # otherwise it is already offered as the main video
    if kind == KIND_VIDEO_SD:
        if not clean:
            return []
        top = clean[0].resolution or 0
        return sorted((f for f in clean if (f.resolution or 0) < top), key=_quality_key, reverse=True)
    return []


@dataclass(frozen=True, slots=True)
class AudioPlan:
    source: MediaFormat
    ext: str  # extension of the file the user receives
    transcode: bool  # run ffmpeg to produce an MP3
    from_video: bool  # the source is a video file and only its audio track is wanted


def audio_plan(info: VideoInfo, *, ffmpeg_available: bool) -> AudioPlan | None:
    """Decide how to deliver the sound, preferring TikTok's own audio file."""
    natives = [f for f in info.formats if f.audio_only]
    natives.sort(key=lambda f: (f.tbr or 0.0, f.filesize or 0), reverse=True)
    native = natives[0] if natives else None

    if native and native.ext == "mp3":
        return AudioPlan(native, "mp3", transcode=False, from_video=False)
    if ffmpeg_available:
        if native:
            return AudioPlan(native, "mp3", transcode=True, from_video=False)
        videos = _video_pool(info)
        if videos:  # any rendition has the same audio, so fetch the smallest
            smallest = min(videos, key=lambda f: (f.resolution or 10**6, f.tbr or 10**6))
            return AudioPlan(smallest, "mp3", transcode=True, from_video=True)
        return None
    if native:
        return AudioPlan(native, native.ext, transcode=False, from_video=False)
    return None


def is_slideshow(info: VideoInfo) -> bool:
    """Photo posts have photos, or at least come back from yt-dlp with a sound track but no video."""
    return bool(info.images) or (not _video_pool(info) and any(f.audio_only for f in info.formats))


# -------------------------------------------------------------------------- options


@dataclass(frozen=True, slots=True)
class DownloadOption:
    kind: str
    title: str
    detail: str
    tag: str = ""
    ext: str = "mp4"
    primary: bool = False


def format_size(num_bytes: int) -> str:
    if num_bytes < 1024 * 1024:
        return f"{max(1, round(num_bytes / 1024))} KB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


def _size_text(fmt: MediaFormat, duration: int | None) -> str:
    if fmt.filesize:
        return format_size(fmt.filesize)
    if fmt.tbr and duration:
        return "~" + format_size(int(fmt.tbr * 125 * duration))  # kbit/s -> bytes/s
    return ""


def _video_option(fmt: MediaFormat, info: VideoInfo, kind: str, *, primary: bool = False, note: str = "") -> DownloadOption:
    title = f"Video · {fmt.resolution}p" if fmt.resolution else "Video"
    detail = " · ".join(p for p in ("MP4", fmt.codec_name, _size_text(fmt, info.duration), note) if p)
    tag = "Watermarked" if fmt.watermarked else "No watermark"
    return DownloadOption(kind, title, detail, tag, "mp4", primary)


def build_options(info: VideoInfo, *, ffmpeg_available: bool) -> list[DownloadOption]:
    options: list[DownloadOption] = []

    if info.images:
        count = len(info.images)
        options.append(DownloadOption(KIND_IMAGES, f"Photos · {count}", "All images in one ZIP", "", "zip", primary=True))

    main = video_candidates(info, KIND_VIDEO)
    if main:
        options.append(_video_option(main[0], info, KIND_VIDEO, primary=True))
    if sd := video_candidates(info, KIND_VIDEO_SD):
        # TikTok's sharpest file is often H.265, which some players (default Windows apps, say) cannot open.
        compatible = bool(main) and main[0].codec_name == "H.265" and sd[0].codec_name == "H.264"
        options.append(_video_option(sd[0], info, KIND_VIDEO_SD, note="plays on any device" if compatible else ""))

    if plan := audio_plan(info, ffmpeg_available=ffmpeg_available):
        detail = info.track or "Sound only"
        primary = not main and not info.images
        options.append(DownloadOption(KIND_AUDIO, f"Audio · {plan.ext.upper()}", detail, "", plan.ext, primary=primary))

    if info.covers:
        options.append(DownloadOption(KIND_COVER, "Cover image", "Thumbnail", "", "jpg"))

    if wm := video_candidates(info, KIND_VIDEO_WM):
        options.append(_video_option(wm[0], info, KIND_VIDEO_WM))

    if options:  # the caption alone is not "something to download"
        options.append(DownloadOption(KIND_META, "Caption & details", "JSON · caption, date, stats, sound", "", "json"))
    return options


# ------------------------------------------------------------------- post details

_HASHTAG = re.compile(r"#(\w+)")


def hashtags(text: str) -> list[str]:
    return list(dict.fromkeys(_HASHTAG.findall(text)))


def _utc(timestamp: int | None) -> datetime | None:
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def iso_date(timestamp: int | None) -> str | None:
    """``2024-03-14``, in UTC so a post's date does not depend on where the server runs."""
    moment = _utc(timestamp)
    return moment.strftime("%Y-%m-%d") if moment else None


def posted_at(info: VideoInfo) -> str | None:
    moment = _utc(info.timestamp)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def posted_date(info: VideoInfo) -> str | None:
    return iso_date(info.timestamp)


def post_kind(info: VideoInfo) -> str:
    if info.images:
        return "photos"
    return "video" if _video_pool(info) else "audio"


def post_record(info: VideoInfo) -> dict[str, Any]:
    """Everything worth keeping about a post besides its media: the caption file and manifest row."""
    caption = info.caption or info.description or info.title
    return {
        "id": info.id,
        "url": info.url,
        "type": post_kind(info),
        "caption": caption,
        "hashtags": hashtags(caption),
        "author": {"name": info.author_name, "handle": info.author_handle},
        "posted_at": posted_at(info),
        "duration": info.duration,
        "stats": dict(info.stats),
        "sound": {"title": info.track, "artists": list(info.artists)},
        "photo_count": len(info.images) or None,
    }


# ------------------------------------------------------------------- profile listing

_POST_ID = re.compile(r"\d{6,25}")


@dataclass(frozen=True, slots=True)
class ProfilePost:
    id: str
    url: str
    caption: str
    timestamp: int | None
    duration: int | None
    thumbnail: str | None
    stats: dict[str, int | None]
    photos: int = 0  # how many photos, if this is a photo post


@dataclass(slots=True)
class ProfileListing:
    handle: str
    name: str | None
    posts: list[ProfilePost]
    more: bool = False  # TikTok has older posts beyond the ones listed
    warning: str | None = None  # the list is shorter than it should be because TikTok pushed back


def parse_profile_entry(entry: Any, handle: str) -> ProfilePost | None:
    """One row of yt-dlp's flat listing of a user. It already carries caption, date and stats."""
    if not isinstance(entry, Mapping):
        return None
    post_id = str(entry.get("id") or "")
    if not _POST_ID.fullmatch(post_id):
        return None
    covers = _cover_urls(entry)
    return ProfilePost(
        id=post_id,
        url=f"https://www.tiktok.com/@{handle}/video/{post_id}",  # rebuilt: never trust the entry's own URL
        caption=_text(entry.get("description") or entry.get("title"), 300),
        timestamp=_timestamp(entry.get("timestamp")),
        duration=_int(entry.get("duration")),
        thumbnail=covers[0] if covers else None,
        stats={
            "views": _int(entry.get("view_count")),
            "likes": _int(entry.get("like_count")),
            "comments": _int(entry.get("comment_count")),
            "shares": _int(entry.get("repost_count")),
        },
        photos=len(_image_urls(entry.get("snaptik_images"))),
    )


# ------------------------------------------------------------------------ filenames

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(stem: str, ext: str) -> str:
    """ASCII-only, so the Content-Disposition header can never be abused or mangled."""
    stem = _UNSAFE.sub("_", stem).strip("._-")[:80] or "tiktok"
    ext = re.sub(r"[^A-Za-z0-9]", "", ext).lower()[:5] or "bin"
    return f"{stem}.{ext}"


def download_stem(info: VideoInfo, suffix: str = "") -> str:
    parts = ["tiktok", info.author_handle, info.id, suffix]
    return "_".join(p for p in parts if p)
