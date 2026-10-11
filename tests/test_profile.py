"""Profile listing, through yt-dlp's real ``TikTokUserIE``.

Only its two network calls are stubbed: the profile web page (which carries the user's secUid)
and the paged ``creator/item_list`` API. yt-dlp's own pagination, retry handling and entry
parsing run for real, so this fails loudly if a yt-dlp upgrade changes what a listing contains.
As with the other canaries, it cannot say whether TikTok's live API still matches the fixture.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from yt_dlp.extractor.tiktok import TikTokUserIE
from yt_dlp.utils import ExtractorError

from app.backend import YtDlpBackend
from app.config import Settings
from app.errors import LoginRequired, UpstreamBlocked, VideoUnavailable
from app.media import ProfilePost, parse_profile_entry

pytestmark = pytest.mark.skipif(
    not hasattr(TikTokUserIE, "_entries"), reason="yt-dlp's TikTok user extractor changed; update this canary"
)

SEC_UID = "MS4wLjABAAAA" + "x" * 64
BASE = 7_300_000_000_000_000_000


def user_page(*, count: int | None = 5, status: int | None = None) -> str:
    detail: dict[str, Any] = {"userInfo": {"user": {"secUid": SEC_UID}, "stats": {"videoCount": count}}}
    if status is not None:
        detail = {"statusCode": status}
    blob = {"__DEFAULT_SCOPE__": {"webapp.user-detail": detail}}
    return f'<html><script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{json.dumps(blob)}</script></html>'


def item(n: int, *, desc: str | None = None, ts: int | None = None, plays: int = 1000) -> dict[str, Any]:
    post_id = str(BASE + n)
    return {
        "id": post_id,
        "desc": desc if desc is not None else f"post number {n} #fyp #cats",
        "createTime": ts if ts is not None else 1_700_000_000 + n * 100,
        "author": {"id": "6666", "uniqueId": "scout2015", "nickname": "Scout", "secUid": SEC_UID},
        "music": {"title": "original sound", "authorName": "Scout", "duration": 15},
        "stats": {"playCount": plays, "diggCount": 5, "shareCount": 1, "commentCount": 2, "collectCount": 0},
        "video": {
            "duration": 15,
            "cover": f"https://p16.example.test/{post_id}-cover.jpeg",
            "originCover": f"https://p16.example.test/{post_id}-origin.jpeg",
            "dynamicCover": f"https://p16.example.test/{post_id}-animated.webp",
        },
    }


def page(*items: dict[str, Any], more: bool) -> dict[str, Any]:
    return {"itemList": list(items), "hasMorePrevious": more}


def backend_for(pages: list[dict[str, Any] | Exception], html: str | None = None) -> tuple[YtDlpBackend, dict[str, int]]:
    calls = {"page": 0, "api": 0}

    class TikTokUserFixtureIE(TikTokUserIE):
        def _download_webpage(self, *args: Any, **kwargs: Any) -> str:
            calls["page"] += 1
            return html if html is not None else user_page()

        def _download_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            calls["api"] += 1
            result = pages[min(calls["api"] - 1, len(pages) - 1)]
            if isinstance(result, Exception):
                raise result
            return result

    class Backend(YtDlpBackend):
        USER_IE_KEY = TikTokUserFixtureIE.ie_key()

        def _new_ydl(self, **overrides: Any):  # type: ignore[no-untyped-def]
            ydl = super()._new_ydl(**overrides)
            ydl.add_info_extractor(TikTokUserFixtureIE())
            return ydl

    return Backend(Settings(network_timeout=5)), calls


TWO_PAGES = [
    page(item(5), item(4), item(3), more=True),
    page(item(2), item(1), more=False),
]


def test_a_profile_is_listed_newest_first_with_captions_dates_and_stats() -> None:
    backend, calls = backend_for(TWO_PAGES)
    listing = backend.list_profile("scout2015", 50)

    assert (listing.handle, listing.name, listing.more, listing.warning) == ("scout2015", "Scout", False, None)
    assert [p.id for p in listing.posts] == [str(BASE + n) for n in (5, 4, 3, 2, 1)]
    assert calls == {"page": 1, "api": 2}

    newest = listing.posts[0]
    assert newest.caption == "post number 5 #fyp #cats"  # the full caption comes with the listing
    assert newest.timestamp == 1_700_000_500 and newest.duration == 15
    assert newest.stats == {"views": 1000, "likes": 5, "comments": 2, "shares": 1}
    assert newest.url == f"https://www.tiktok.com/@scout2015/video/{BASE + 5}"
    # Static covers come before the animated one, and the original-size cover outranks the small one.
    assert newest.thumbnail == f"https://p16.example.test/{BASE + 5}-origin.jpeg"


def test_a_small_limit_stops_paging_and_says_there_is_more() -> None:
    backend, calls = backend_for(TWO_PAGES)
    listing = backend.list_profile("scout2015", 2)
    assert [p.id for p in listing.posts] == [str(BASE + 5), str(BASE + 4)]
    assert listing.more is True
    assert calls["api"] == 1  # the second page was never requested


def test_a_limit_equal_to_the_total_is_not_reported_as_truncated() -> None:
    backend, _ = backend_for(TWO_PAGES)
    listing = backend.list_profile("scout2015", 5)
    assert len(listing.posts) == 5 and listing.more is False


def test_duplicate_posts_across_pages_are_listed_once() -> None:
    backend, _ = backend_for([page(item(3), item(2), more=True), page(item(2), item(1), more=False)])
    assert [p.id for p in backend.list_profile("scout2015", 50).posts] == [str(BASE + n) for n in (3, 2, 1)]


def test_a_failure_on_a_later_page_keeps_what_was_already_listed() -> None:
    backend, _ = backend_for([page(item(3), item(2), more=True), ExtractorError("HTTP Error 429: Too Many Requests")])
    listing = backend.list_profile("scout2015", 50)
    assert [p.id for p in listing.posts] == [str(BASE + 3), str(BASE + 2)]
    assert listing.more is True and "stopped the list early" in (listing.warning or "")


def test_a_failure_on_the_first_page_is_an_error() -> None:
    backend, _ = backend_for([ExtractorError("HTTP Error 403: Forbidden")])
    with pytest.raises(UpstreamBlocked):
        backend.list_profile("scout2015", 50)


def test_a_private_account_is_reported_as_such() -> None:
    backend, calls = backend_for(TWO_PAGES, html=user_page(status=10222))
    with pytest.raises(LoginRequired, match="private"):
        backend.list_profile("scout2015", 50)
    assert calls["api"] == 0


def test_an_account_without_posts_is_reported_as_such() -> None:
    backend, _ = backend_for(TWO_PAGES, html=user_page(count=0))
    with pytest.raises(VideoUnavailable, match="hasn't posted"):
        backend.list_profile("scout2015", 50)


def test_the_user_extractor_is_the_only_thing_asked_for_a_profile() -> None:
    backend, calls = backend_for(TWO_PAGES)
    backend.list_profile("scout2015", 50)
    assert calls["page"] == 1  # one profile page load, then the paged API


# ---------------------------------------------------------------- entry parsing


def test_entries_without_a_usable_id_are_skipped() -> None:
    assert parse_profile_entry({"id": "abc"}, "u") is None
    assert parse_profile_entry({"id": "123"}, "u") is None  # too short to be a post id
    assert parse_profile_entry("nope", "u") is None
    assert parse_profile_entry({}, "u") is None


def test_an_entry_is_reduced_to_safe_known_fields() -> None:
    post = parse_profile_entry(
        {
            "id": str(BASE),
            "url": "https://evil.example/steal",  # never used: the URL is rebuilt from the validated handle
            "description": "  hi\nthere  ",
            "view_count": True,  # bools are not numbers
            "thumbnails": [{"url": "javascript:alert(1)"}, {"url": "https://cdn.example.test/a.jpg"}],
        },
        "scout2015",
    )
    assert post == ProfilePost(
        id=str(BASE),
        url=f"https://www.tiktok.com/@scout2015/video/{BASE}",
        caption="hi there",
        timestamp=None,
        duration=None,
        thumbnail="https://cdn.example.test/a.jpg",
        stats={"views": None, "likes": None, "comments": None, "shares": None},
    )
