"""The whole chain in one go: the command-line saver, the archive loop, the real ``YtDlpBackend`` and
yt-dlp's real TikTok extractors, a local stand-in for the CDN, and files on a real disk.

Only what needs the internet is replaced: the profile page and listing API, the post page data, and the
CDN. The other tests check each piece on its own; this one checks the seams between them (for example
that the date yt-dlp reads from a post is what ends up in its file name, and that a profile is saved
with one TikTok page load per post). Like the other canaries it cannot say whether TikTok's live
site still looks like the fixtures.
"""
from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Any

import pytest
from yt_dlp.extractor.tiktok import TikTokIE, TikTokUserIE

from app.backend import TikTokPhotosIE, YtDlpBackend
from app.bulk import EXIT_BLOCKED, EXIT_OK, main
from app.config import Settings
from tests.helpers import Cdn
from tests.test_profile import BASE, item, page, user_page
from tests.test_ytdlp_tiktok_parser import page_blob, photo_blob

pytestmark = pytest.mark.skipif(
    not (hasattr(TikTokIE, "_extract_web_data_and_status") and hasattr(TikTokUserIE, "_entries")),
    reason="yt-dlp's TikTok extractors changed under the hooks these canaries stub; update the tests",
)

DAY = 86_400
START = 1_700_000_000  # 2023-11-14 22:13:20 UTC


def post(n: int) -> str:
    return str(BASE + n)


def make_backend(cdn: Cdn, blobs: dict[str, tuple[dict[str, Any], int]], listed: list[int]) -> tuple[YtDlpBackend, list[str]]:
    """``blobs`` maps a post id to the (page data, status) its TikTok page would return."""
    page_loads: list[str] = []

    class UserFixtureIE(TikTokUserIE):
        def _download_webpage(self, *args: Any, **kwargs: Any) -> str:
            return user_page(count=len(listed))

        def _download_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            return page(*(item(n, ts=START + n * DAY) for n in listed), more=False)

    class PostFixtureIE(TikTokPhotosIE):
        def _extract_web_data_and_status(self, url: str, video_id: str, fatal: bool = True):  # type: ignore[override]
            page_loads.append(video_id)
            blob, status = blobs[video_id]
            return blob, status

    class Backend(YtDlpBackend):
        IE_KEY = PostFixtureIE.ie_key()
        USER_IE_KEY = UserFixtureIE.ie_key()

        def _new_ydl(self, **overrides: Any):  # type: ignore[no-untyped-def]
            ydl = super()._new_ydl(**overrides)
            ydl.add_info_extractor(UserFixtureIE())
            ydl.add_info_extractor(PostFixtureIE())
            return ydl

    return Backend(Settings(allow_private_upstream=True, network_timeout=5)), page_loads


def blobs_for(cdn: Cdn) -> dict[str, tuple[dict[str, Any], int]]:
    def video(n: int, desc: str) -> dict[str, Any]:
        return page_blob(cdn, id=post(n), desc=desc, createTime=str(START + n * DAY))

    return {
        post(4): (video(4, "=1+1 is not a formula #fyp"), 0),
        post(3): (photo_blob(cdn, id=post(3), desc="photo dump 📸 #fyp", createTime=str(START + 3 * DAY)), 0),
        post(2): ({}, 10216),  # TikTok's status for a private post: yt-dlp reports it as needing a login
        post(1): (video(1, "my very first post"), 0),
    }


def run(folder: Path, backend: YtDlpBackend, *args: str, sleeps: list[float] | None = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    sleep = sleeps.append if sleeps is not None else (lambda _: None)
    code = main(["@scout2015", "--out", str(folder), *args], backend=backend, sleep=sleep, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def files_under(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file())


def test_a_profile_is_saved_end_to_end_and_a_second_run_does_nothing(cdn: Cdn, media: dict[str, bytes], tmp_path: Path) -> None:
    folder = tmp_path / "scout"
    backend, loads = make_backend(cdn, blobs_for(cdn), listed=[4, 3, 2, 1])

    code, out, err = run(folder, backend, "--what", "media,audio,cover")
    assert (code, err) == (EXIT_OK, "")
    assert "4 posts listed (2023-11-15 to 2023-11-18); 0 already saved or settled; 4 to fetch." in out
    assert "Saved 3 of 4, 1 unavailable" in out
    assert loads == [post(4), post(3), post(2), post(1)]  # one TikTok page load per post, however many files it has

    video4 = f"2023-11-18_{post(4)}_1-1-is-not-a-formula-fyp"
    video1 = f"2023-11-15_{post(1)}_my-very-first-post"
    photos3 = f"2023-11-17_{post(3)}_photo-dump-fyp"
    assert files_under(folder) == sorted([
        f"{video4}.mp4", f"{video4}_sound.mp3", f"{video4}_cover.jpg",
        f"{video1}.mp4", f"{video1}_sound.mp3", f"{video1}_cover.jpg",
        f"{photos3}/01.jpg", f"{photos3}/02.jpg", f"{photos3}/03.png", f"{photos3}/sound.mp3", f"{photos3}/cover.jpg",
        "posts.json", "posts.csv", "posts.jsonl", "REPORT.txt",
    ])  # fmt: skip

    # The bytes are the CDN's, picked and fetched by the real extractor and downloader.
    assert (folder / f"{video4}.mp4").read_bytes() == media["video.mp4"]  # the 1080p H.264 rendition, via a live host
    assert (folder / f"{video4}_sound.mp3").read_bytes() == media["sound.mp3"]
    assert (folder / f"{video4}_cover.jpg").read_bytes() == media["cover.jpg"]
    assert (folder / photos3 / "01.jpg").read_bytes() == media["cover.jpg"]  # the first photo's dead CDN host was skipped
    assert (folder / photos3 / "02.jpg").read_bytes() == media["photo2.jpg"]
    assert (folder / photos3 / "03.png").read_bytes() == media["photo3.png"]
    assert os.stat(folder / f"{video4}.mp4").st_mtime == START + 4 * DAY  # the file is dated when the post was published

    manifest = json.loads((folder / "posts.json").read_text(encoding="utf-8"))
    assert [(r["id"], r["status"], r.get("type")) for r in manifest] == [
        (post(4), "ok", "video"), (post(3), "ok", "photos"), (post(1), "ok", "video"), (post(2), "skipped", None),
    ]  # newest first; a post with no date (the private one) sorts last
    assert manifest[0]["caption"].startswith("=1+1 is not a formula")
    assert manifest[0]["stats"] == {"views": 1234, "likes": 56, "comments": 8, "shares": 7}
    assert manifest[1]["photo_count"] == 3 and manifest[1]["caption"] == "photo dump 📸 #fyp"
    assert manifest[3]["reason"].startswith("TikTok requires a login to view this post")

    with (folder / "posts.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["caption"] == "'=1+1 is not a formula #fyp"  # safe to open in a spreadsheet
    assert (folder / "REPORT.txt").read_text(encoding="utf-8").count(post(2)) == 1

    # Running it again: everything is settled, so TikTok is not asked about a single post.
    code, out, _ = run(folder, backend, "--what", "media,audio,cover")
    assert code == EXIT_OK and "Nothing to do." in out and "0 to fetch" in out
    assert len(loads) == 4

    # ... until the private post is asked for again on purpose.
    run(folder, backend, "--retry-skipped")
    assert loads[4:] == [post(2)]


def test_a_profile_with_a_post_that_fails_halfway_can_be_finished_later(cdn: Cdn, media: dict[str, bytes], tmp_path: Path) -> None:
    """TikTok is fine with the listing but a post's page errors out once: the run records it and the next run completes it."""
    folder = tmp_path / "scout"
    blobs = blobs_for(cdn)
    backend, loads = make_backend(cdn, blobs, listed=[4, 1])
    real = blobs[post(4)]
    blobs[post(4)] = ({}, 10000)  # yt-dlp: "Video not available, status code 10000": settled, so it is skipped

    code, out, _ = run(folder, backend)
    assert code == EXIT_OK and "Saved 1 of 2, 1 unavailable" in out
    assert [p.name for p in folder.glob("*.mp4")] == [f"2023-11-15_{post(1)}_my-very-first-post.mp4"]

    blobs[post(4)] = real  # the post is back
    run(folder, backend)
    assert len(loads) == 2  # settled posts are not asked about again...
    run(folder, backend, "--retry-skipped")
    assert loads[2:] == [post(4)]  # ... but the skipped one is, on request, and now it is saved
    assert len(list(folder.glob("*.mp4"))) == 2
    assert [r["status"] for r in json.loads((folder / "posts.json").read_text(encoding="utf-8"))] == ["ok", "ok"]


def test_when_tiktok_says_the_ip_is_blocked_the_run_backs_off_then_stops_cleanly(cdn: Cdn, tmp_path: Path) -> None:
    """yt-dlp's real wording for a blocked IP must be recognised as a block (not as 'post gone')."""
    folder = tmp_path / "scout"
    blobs = {post(2): ({}, 10204), post(1): ({}, 10204)}  # "Your IP address is blocked from accessing this post"
    backend, loads = make_backend(cdn, blobs, listed=[2, 1])
    sleeps: list[float] = []

    code, _, err = run(folder, backend, sleeps=sleeps)

    assert code == EXIT_BLOCKED
    assert loads == [post(2)] * 6  # the first attempt and five retries; the second post is never even tried
    waits = (60, 120, 240, 480, 900)  # doubling from a minute, then capped at --max-backoff, each up to a quarter longer
    assert len(sleeps) == 5 and all(base <= s <= base * 1.25 for s, base in zip(sleeps, waits, strict=True))
    assert "Stopped early: TikTok keeps refusing requests" in err and "Run the same command again later" in err
    report = (folder / "REPORT.txt").read_text(encoding="utf-8")
    assert "The run ended early" in report and "Saved 0 of 1 posts" in report
    assert not list(folder.glob("*.mp4"))
