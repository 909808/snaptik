"""HTTP-level tests: routes, error mapping, limits and security headers, over a fake backend."""
from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import FormatUnavailable, LoginRequired, TooLarge, UpstreamBlocked, VideoUnavailable
from app.main import create_app
from tests.fakes import CANONICAL, VIDEO_ID, FakeBackend, make_info, sample_formats

LINK = f"https://www.tiktok.com/@scout2015/video/{VIDEO_ID}"


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(Settings(), backend := FakeBackend())
    with TestClient(app) as c:
        c.backend = backend  # type: ignore[attr-defined]
        c.service = app.state.service  # type: ignore[attr-defined]
        yield c


def lookup(client: TestClient, text: str = LINK):
    return client.post("/api/info", json={"url": text})


# ---------------------------------------------------------------------- pages


def test_home_page_is_served_with_security_headers(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "SnapTik" in response.text
    assert response.headers["cache-control"] == "no-cache"
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp and "'unsafe-eval'" not in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert client.head("/").status_code == 200  # monitors and link checkers use HEAD


def test_static_assets_and_health(client: TestClient) -> None:
    for path in ("/static/app.js", "/static/style.css", "/static/theme.js", "/static/favicon.svg", "/robots.txt"):
        assert client.get(path).status_code == 200, path
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.head("/healthz").status_code == 200
    assert "Disallow: /api/" in client.get("/robots.txt").text


def test_static_assets_revalidate_instead_of_going_stale(client: TestClient) -> None:
    first = client.get("/static/app.js")
    assert first.headers["cache-control"] == "no-cache" and first.headers["etag"]
    again = client.get("/static/app.js", headers={"if-none-match": first.headers["etag"]})
    assert again.status_code == 304


def test_page_has_no_inline_script_or_style_so_the_strict_csp_holds(client: TestClient) -> None:
    html = client.get("/").text
    assert "<script>" not in html and "onclick=" not in html and "style=" not in html


def test_the_page_script_never_turns_untrusted_text_into_markup(client: TestClient) -> None:
    """Captions, names and titles come from TikTok users: they must only ever reach the page as text."""
    script = client.get("/static/app.js").text
    for dangerous in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert dangerous not in script, dangerous


def test_page_has_the_profile_controls(client: TestClient) -> None:
    html = client.get("/").text
    for element_id in ("profile", "p-list", "p-download", "p-stop", "p-bar", "w-media", "w-audio", "w-cover", "r-back"):
        assert f'id="{element_id}"' in html, element_id


def test_api_docs_are_not_exposed(client: TestClient) -> None:
    assert all(client.get(p).status_code == 404 for p in ("/docs", "/redoc", "/openapi.json"))


# --------------------------------------------------------------------- lookup


def test_lookup_describes_the_post_and_its_downloads(client: TestClient) -> None:
    response = lookup(client)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    assert data["id"] == VIDEO_ID and data["url"] == CANONICAL
    assert data["author"] == {"name": "Scout", "handle": "scout2015"}
    assert data["duration"] == 15 and data["note"] is None
    assert data["stats"]["views"] == 1_234_567
    assert [d["kind"] for d in data["downloads"]] == ["video", "video_sd", "audio", "cover", "video_wm", "meta"]
    assert [d["primary"] for d in data["downloads"]] == [True, False, False, False, False, False]


def test_download_links_point_back_at_the_api_with_the_canonical_url(client: TestClient) -> None:
    for item in lookup(client).json()["downloads"]:
        parts = urlsplit(item["href"])
        query = parse_qs(parts.query)
        assert parts.path == "/api/download"
        assert query == {"url": [CANONICAL], "kind": [item["kind"]]}


def test_lookup_accepts_pasted_share_text(client: TestClient) -> None:
    response = lookup(client, f"Look at this!! {LINK}?is_from_webapp=1 #fyp")
    assert response.status_code == 200
    assert client.backend.fetch_calls == [CANONICAL]  # the query string never reaches the backend


def test_repeated_lookups_are_served_from_the_cache(client: TestClient) -> None:
    for _ in range(3):
        assert lookup(client).status_code == 200
    assert len(client.backend.fetch_calls) == 1


@pytest.mark.parametrize(
    "body",
    [{"url": "https://example.com/video"}, {"url": "hello"}, {"url": "javascript:alert(1)"},
     {"url": "http://169.254.169.254/latest/meta-data"}],
)  # fmt: skip
def test_non_tiktok_input_is_rejected_without_touching_the_backend(client: TestClient, body: dict) -> None:
    response = client.post("/api/info", json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_url"
    assert client.backend.fetch_calls == []


@pytest.mark.parametrize("payload", [{}, {"url": ""}, {"url": 5}, {"wrong": "x"}, {"url": "x" * 9000}])
def test_malformed_requests_get_a_json_400(client: TestClient, payload: dict) -> None:
    response = client.post("/api/info", json=payload)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_non_json_body_is_a_json_400(client: TestClient) -> None:
    response = client.post("/api/info", content="url=abc", headers={"content-type": "text/plain"})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (VideoUnavailable(), 404, "video_unavailable"),
        (LoginRequired(), 403, "login_required"),
        (UpstreamBlocked(), 502, "upstream_blocked"),
    ],
)
def test_backend_errors_become_friendly_json(error, status: int, code: str) -> None:
    app = create_app(Settings(), FakeBackend(error=error))
    with TestClient(app) as client:
        response = lookup(client)
    assert response.status_code == status
    body = response.json()["error"]
    assert body["code"] == code and body["message"] and "Traceback" not in body["message"]
    assert response.headers["content-security-policy"]  # error responses carry the headers too


def test_unexpected_exceptions_do_not_leak_details() -> None:
    class Exploding(FakeBackend):
        def fetch_info(self, url: str):
            raise RuntimeError("secret internal detail")

    app = create_app(Settings(), Exploding())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = lookup(client)
    assert response.status_code == 500
    assert "secret" not in response.text and response.json()["error"]["code"] == "internal_error"


def test_slideshow_posts_explain_what_can_be_downloaded() -> None:
    info = make_info(formats=[f for f in sample_formats() if f.audio_only])
    app = create_app(Settings(), FakeBackend(info))
    with TestClient(app) as client:
        data = lookup(client).json()
    assert [d["kind"] for d in data["downloads"]] == ["audio", "cover", "meta"]
    assert "slideshow" in data["note"]


def test_a_post_with_nothing_to_download_is_reported_as_such() -> None:
    app = create_app(Settings(), FakeBackend(make_info(formats=[], covers=[], thumbnail=None)))
    with TestClient(app) as client:
        response = lookup(client)
    assert response.status_code == 404 and response.json()["error"]["code"] == "format_unavailable"


def test_audio_option_follows_ffmpeg_availability() -> None:
    app = create_app(Settings(), FakeBackend(ffmpeg_available=False))
    with TestClient(app) as client:
        audio = next(d for d in lookup(client).json()["downloads"] if d["kind"] == "audio")
    assert audio["title"] == "Audio · M4A"


def test_short_links_are_resolved_before_the_backend_is_asked() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "vm.tiktok.com"
        return httpx.Response(301, headers={"location": f"{LINK}?_r=1"})

    app = create_app(Settings(), backend := FakeBackend())
    app.state.service.http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    with TestClient(app) as client:
        response = lookup(client, "https://vm.tiktok.com/ZMabcdef/")
    assert response.status_code == 200 and backend.fetch_calls == [CANONICAL]


# ------------------------------------------------------------------- download


def download(client: TestClient, kind: str = "video", url: str = CANONICAL, **extra: str):
    return client.get("/api/download", params={"url": url, "kind": kind, **extra})


def test_download_streams_the_file_as_an_attachment(client: TestClient) -> None:
    response = download(client)
    assert response.status_code == 200
    assert response.content == client.backend.payload
    assert response.headers["content-disposition"] == 'attachment; filename="fake_video.mp4"'
    assert response.headers["content-length"] == str(len(client.backend.payload))
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["cache-control"] == "no-store"
    assert client.backend.open_calls == [(CANONICAL, "video")]
    assert client.backend.closed == 1  # the upstream connection was released


def test_download_normalises_whatever_url_it_is_given(client: TestClient) -> None:
    assert download(client, url=f"{LINK}?is_from_webapp=1&x=y").status_code == 200
    assert client.backend.open_calls == [(CANONICAL, "video")]


@pytest.mark.parametrize("kind", ["video", "video_sd", "video_wm", "audio", "cover"])
def test_every_kind_the_lookup_offers_can_be_requested(client: TestClient, kind: str) -> None:
    assert download(client, kind).status_code == 200
    assert client.backend.open_calls[-1] == (CANONICAL, kind)


@pytest.mark.parametrize("kind", ["", "VIDEO", "../etc/passwd", "video;rm", "x" * 40])
def test_unknown_kinds_are_rejected(client: TestClient, kind: str) -> None:
    assert download(client, kind).status_code == 400
    assert client.backend.open_calls == []


def test_download_of_a_non_tiktok_url_is_rejected(client: TestClient) -> None:
    response = download(client, url="https://evil.example/@a/video/123456789")
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_url"
    assert client.backend.open_calls == []


def test_download_requires_a_url(client: TestClient) -> None:
    response = client.get("/api/download", params={"kind": "video"})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"


def test_progress_cookie_is_set_only_for_well_formed_nonces(client: TestClient) -> None:
    assert "dl_abcd1234=1" in download(client, n="abcd1234").headers["set-cookie"]
    assert "set-cookie" not in download(client, n="bad nonce!").headers
    assert "set-cookie" not in download(client).headers


@pytest.mark.parametrize(
    ("error", "status"), [(TooLarge(), 413), (FormatUnavailable(), 404), (VideoUnavailable(), 404), (UpstreamBlocked(), 502)]
)
def test_download_errors_are_json_and_do_not_leak_a_slot(error, status: int) -> None:
    backend = FakeBackend(error=error)
    app = create_app(Settings(max_concurrent_downloads=1), backend)
    with TestClient(app) as client:
        for _ in range(3):  # would hit "busy" on the 2nd try if a failed attempt kept its slot
            response = download(client)
            assert response.status_code == status
            assert response.headers["content-type"].startswith("application/json")


def test_downloads_beyond_the_concurrency_limit_get_a_retryable_503() -> None:
    app = create_app(Settings(max_concurrent_downloads=1), FakeBackend())
    with TestClient(app) as client:
        gate = app.state.service._download_gate
        assert gate.acquire()  # someone else is mid-download
        busy = download(client)
        assert busy.status_code == 503 and busy.headers["retry-after"] == "5"
        assert busy.json()["error"]["code"] == "busy"
        gate.release()
        assert download(client).status_code == 200


def test_slot_is_returned_after_a_successful_download() -> None:
    app = create_app(Settings(max_concurrent_downloads=1), FakeBackend())
    with TestClient(app) as client:
        assert all(download(client).status_code == 200 for _ in range(3))


# ------------------------------------------------------------------ rate limits


def test_lookups_are_rate_limited_per_client() -> None:
    app = create_app(Settings(rate_limit_lookup=2), FakeBackend())
    with TestClient(app) as client:
        assert [lookup(client).status_code for _ in range(2)] == [200, 200]
        limited = lookup(client)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(limited.headers["retry-after"]) <= 60


def test_downloads_have_their_own_budget() -> None:
    app = create_app(Settings(rate_limit_lookup=1, rate_limit_download=2), FakeBackend())
    with TestClient(app) as client:
        assert lookup(client).status_code == 200
        assert lookup(client).status_code == 429
        assert [download(client).status_code for _ in range(2)] == [200, 200]
        assert download(client).status_code == 429


def test_the_same_video_with_a_different_handle_shares_one_cache_entry() -> None:
    app = create_app(Settings(), backend := FakeBackend())
    with TestClient(app) as client:
        lookup(client, f"https://www.tiktok.com/@scout2015/video/{VIDEO_ID}")
        lookup(client, f"https://www.tiktok.com/@someone_else/video/{VIDEO_ID}")
    assert len(backend.fetch_calls) == 1


# ---------------------------------------------------------- real network stack


def test_a_client_that_disconnects_mid_download_releases_everything(unused_tcp_port: int) -> None:
    """Runs a real uvicorn server: TestClient cannot simulate a dropped connection.

    The source is slow (5 ms per 64 KB read), so a server that kept going for a client that had left
    would need seconds to drain it. It must stop almost at once, having read only a small part."""
    import threading
    import time

    import uvicorn

    from app.backend import Download

    reads = 0
    total_reads = 480  # about 30 MB

    class SlowBackend(FakeBackend):
        def open_download(self, url: str, kind: str) -> Download:
            self.open_calls.append((url, kind))

            def read(size: int) -> bytes:
                nonlocal reads
                if reads >= total_reads:
                    return b""
                reads += 1
                time.sleep(0.005)
                return b"x" * size

            return Download(filename="slow.mp4", media_type="video/mp4", length=None, reader=read,
                            cleanups=[self._count_close])  # fmt: skip

    backend = SlowBackend()
    app = create_app(Settings(max_concurrent_downloads=1), backend)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=unused_tcp_port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started
        with (
            httpx.Client(timeout=10, trust_env=False) as http,
            http.stream("GET", f"http://127.0.0.1:{unused_tcp_port}/api/download",
                        params={"url": CANONICAL, "kind": "video"}) as response,
        ):  # fmt: skip
            assert response.status_code == 200
            assert next(response.iter_raw())
        # leaving the block closed the socket after the first network read
        deadline = time.time() + 10
        while backend.closed == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert backend.closed == 1, "upstream/temp resources were not released after the client left"
        assert reads < total_reads / 2, f"the server kept reading for a client that had gone ({reads} of {total_reads})"
        assert app.state.service._download_gate.acquire(), "the concurrency slot leaked"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def unused_tcp_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
