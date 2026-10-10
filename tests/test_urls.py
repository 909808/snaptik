"""URL parsing is the trust boundary: these tests include hostile inputs on purpose."""
from __future__ import annotations

import httpx
import pytest

from app.errors import InvalidURL, UpstreamError
from app.urls import MAX_REDIRECTS, Target, parse_input, parse_tiktok_url, resolve_target

VID = "6718335390845095173"


@pytest.mark.parametrize(
    ("url", "user"),
    [
        (f"https://www.tiktok.com/@scout2015/video/{VID}", "scout2015"),
        (f"https://www.tiktok.com/@scout2015/video/{VID}?is_from_webapp=1&sender_device=pc", "scout2015"),
        (f"https://www.tiktok.com/@scout2015/video/{VID}/", "scout2015"),
        (f"http://tiktok.com/@a.b-c_d/video/{VID}", "a.b-c_d"),
        (f"https://m.tiktok.com/@u/video/{VID}", "u"),
        (f"HTTPS://WWW.TIKTOK.COM/@Scout/video/{VID}", "Scout"),
        (f"https://www.tiktok.com/@u/photo/{VID}", "u"),
        (f"https://www.tiktok.com/@/video/{VID}", None),
        (f"https://www.tiktok.com/embed/{VID}", None),
        (f"https://www.tiktok.com/embed/v2/{VID}", None),
        (f"https://www.tiktok.com/share/video/{VID}/", None),
        (f"https://www.tiktokv.com/share/video/{VID}/", None),
        (f"https://m.tiktok.com/v/{VID}.html", None),
    ],
)
def test_video_urls_are_recognised(url: str, user: str | None) -> None:
    target = parse_tiktok_url(url)
    assert target == Target(video_id=VID, user=user)
    assert not target.is_short
    assert target.canonical_url == f"https://www.tiktok.com/@{user or '_'}/video/{VID}"


@pytest.mark.parametrize(
    "url",
    [
        "https://vm.tiktok.com/ZMabcdef/",
        "https://vt.tiktok.com/ZSabc123",
        "https://www.tiktok.com/t/ZTabc123/",
        "https://vm.tiktok.com/ZMabcdef/?foo=bar",
    ],
)
def test_short_links_are_recognised_and_normalised(url: str) -> None:
    target = parse_tiktok_url(url)
    assert target.is_short
    assert target.short_url and "?" not in target.short_url
    with pytest.raises(ValueError):
        _ = target.canonical_url


@pytest.mark.parametrize(
    "url",
    [
        # wrong or look-alike hosts
        f"https://evil.com/@a/video/{VID}",
        f"https://tiktok.com.evil.com/@a/video/{VID}",
        f"https://eviltiktok.com/@a/video/{VID}",
        f"https://www.tiktok.com.evil.com/@a/video/{VID}",
        f"https://tiktok.com@evil.com/@a/video/{VID}",
        f"https://evil.com\\@www.tiktok.com/@a/video/{VID}",
        f"https://user:pw@www.tiktok.com/@a/video/{VID}",
        f"https://evil.com/?u=https://www.tiktok.com/@a/video/{VID}",
        f"https://tіktok.com/@a/video/{VID}",  # Cyrillic і
        f"https://xn--tiktk-9ua.com/@a/video/{VID}",
        "http://127.0.0.1/@a/video/123456789",
        "http://[::1]/@a/video/123456789",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:8000/",
        # wrong scheme or port
        f"ftp://www.tiktok.com/@a/video/{VID}",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,hi",
        f"https://www.tiktok.com:8443/@a/video/{VID}",
        f"https://www.tiktok.com:99999/@a/video/{VID}",
        # right host, not a video
        "https://www.tiktok.com/",
        "https://www.tiktok.com/@scout2015",
        "https://www.tiktok.com/login",
        "https://www.tiktok.com/@a/video/abc",
        "https://www.tiktok.com/@a/video/",
        "https://vm.tiktok.com/",
        "https://www.tiktok.com/@a/video/12",  # too short to be an id
        f"https://www.tiktok.com/@a b/video/{VID}",
        # junk
        "",
        "   ",
        "not a url",
        "https://",
        "https://www.tiktok.com/" + "a" * 3000,
    ],
)
def test_hostile_or_irrelevant_urls_are_rejected(url: str) -> None:
    with pytest.raises(InvalidURL):
        parse_tiktok_url(url)


def test_canonical_url_is_rebuilt_from_validated_parts() -> None:
    """Nothing from the query string or fragment can leak into what we hand to yt-dlp."""
    target = parse_tiktok_url(f"https://www.tiktok.com/@u/video/{VID}?x=https://evil.com#frag")
    assert target.canonical_url == f"https://www.tiktok.com/@u/video/{VID}"


# --------------------------------------------------------------- free-form input


def test_share_text_is_searched_for_a_link() -> None:
    text = "Check out Scout's video! https://vm.tiktok.com/ZMabcdef/ #fyp #cats"
    assert parse_input(text).short_url == "https://vm.tiktok.com/ZMabcdef/"


def test_first_tiktok_link_wins_over_other_links() -> None:
    text = f"see https://example.com/page and https://www.tiktok.com/@a/video/{VID}"
    assert parse_input(text).video_id == VID


@pytest.mark.parametrize(
    "text",
    [
        f"tiktok.com/@a/video/{VID}",
        f"www.tiktok.com/@a/video/{VID}",
        f"(https://www.tiktok.com/@a/video/{VID}).",
        f'"https://www.tiktok.com/@a/video/{VID}",',
        f"<https://www.tiktok.com/@a/video/{VID}>",
    ],
)
def test_bare_and_punctuated_links(text: str) -> None:
    assert parse_input(text).video_id == VID


def test_bare_short_link() -> None:
    assert parse_input("vm.tiktok.com/ZMabc123/").is_short


@pytest.mark.parametrize("text", ["", "hello world", "https://example.com/x", "tiktok", "x" * 9000])
def test_text_without_a_tiktok_link_is_rejected(text: str) -> None:
    with pytest.raises(InvalidURL):
        parse_input(text)


def test_a_look_alike_domain_inside_text_is_not_picked_up() -> None:
    with pytest.raises(InvalidURL):
        parse_input(f"eviltiktok.com/@a/video/{VID}")


# ------------------------------------------------------------ short-link resolving


def make_client(handler) -> tuple[httpx.Client, list[str]]:
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    return httpx.Client(transport=httpx.MockTransport(record), follow_redirects=False), seen


def redirect(location: str, status: int = 301) -> httpx.Response:
    return httpx.Response(status, headers={"location": location})


def test_short_link_is_followed_to_the_video() -> None:
    client, seen = make_client(lambda r: redirect(f"https://www.tiktok.com/@u/video/{VID}?_r=1&u_code=abc"))
    target = resolve_target("https://vm.tiktok.com/ZMabcdef/", client)
    assert target == Target(video_id=VID, user="u")
    assert seen == ["https://vm.tiktok.com/ZMabcdef/"]


def test_chain_of_redirects_is_followed_hop_by_hop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "vm.tiktok.com":
            return redirect("https://www.tiktok.com/t/ZTxyz789/")
        return redirect(f"/@u/video/{VID}", 302)  # relative Location

    client, seen = make_client(handler)
    assert resolve_target("https://vm.tiktok.com/ZMabcdef/", client).video_id == VID
    assert len(seen) == 2


def test_a_full_video_url_needs_no_network() -> None:
    client, seen = make_client(lambda r: pytest.fail("no request expected"))
    assert resolve_target(f"https://www.tiktok.com/@u/video/{VID}", client).video_id == VID
    assert seen == []


def test_redirect_away_from_tiktok_is_rejected_and_never_fetched() -> None:
    client, seen = make_client(lambda r: redirect("http://169.254.169.254/latest/meta-data/"))
    with pytest.raises(InvalidURL):
        resolve_target("https://vm.tiktok.com/ZMabcdef/", client)
    assert seen == ["https://vm.tiktok.com/ZMabcdef/"]  # the evil target was only inspected, not requested


def test_redirect_to_a_non_video_page_is_rejected() -> None:
    client, _ = make_client(lambda r: redirect("https://www.tiktok.com/login?redirect_url=x"))
    with pytest.raises(InvalidURL):
        resolve_target("https://vm.tiktok.com/ZMabcdef/", client)


def test_redirect_loops_are_cut_off() -> None:
    client, seen = make_client(lambda r: redirect("https://vm.tiktok.com/ZMloop123/"))
    with pytest.raises(InvalidURL):
        resolve_target("https://vm.tiktok.com/ZMloop123/", client)
    assert len(seen) == MAX_REDIRECTS


@pytest.mark.parametrize("response", [httpx.Response(200, text="<html>captcha</html>"), httpx.Response(302)])
def test_short_link_that_does_not_redirect_is_an_upstream_error(response: httpx.Response) -> None:
    client, _ = make_client(lambda r: response)
    with pytest.raises(UpstreamError):
        resolve_target("https://vm.tiktok.com/ZMabcdef/", client)


def test_network_failure_while_resolving_is_an_upstream_error() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    client, _ = make_client(boom)
    with pytest.raises(UpstreamError):
        resolve_target("https://vm.tiktok.com/ZMabcdef/", client)
