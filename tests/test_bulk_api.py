"""Profile results and the bulk ZIP over HTTP, with a fake backend and no real pauses."""
from __future__ import annotations

import io
import json
import socket
import threading
import time
import zipfile
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import LoginRequired, UpstreamBlocked, VideoUnavailable
from app.main import create_app
from tests.fakes import FakeBackend, make_listing, post_id

PROFILE = "https://www.tiktok.com/@scout2015"
JOB = "abcdefghij0123456789"  # 20 characters: the browser sends a random one per download


def settings(**overrides: Any) -> Settings:
    return Settings(**{"bulk_delay": 0.0, "rate_limit_lookup": 0, "rate_limit_download": 0, "rate_limit_bulk": 0, **overrides})


def make(backend: FakeBackend | None = None, **overrides: Any) -> tuple[TestClient, FakeBackend, Any]:
    backend = backend or FakeBackend(listing=make_listing(5))
    app = create_app(settings(**overrides), backend)
    return TestClient(app), backend, app.state.service


@pytest.fixture
def client() -> Iterator[tuple[TestClient, FakeBackend, Any]]:
    c, backend, service = make()
    with c:
        yield c, backend, service


def bulk(c: TestClient, ids: list[int] | list[str], what: str = "media", job: str = JOB, user: str = "scout2015"):
    ids = [post_id(i) if isinstance(i, int) else i for i in ids]
    return c.get("/api/bulk", params={"user": user, "ids": ",".join(ids), "what": what, "n": job})


def names(response: httpx.Response) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        return archive.namelist()


# ------------------------------------------------------------------ profile lookup


def test_a_profile_link_returns_the_list_of_posts(client) -> None:
    c, backend, _ = client
    response = c.post("/api/info", json={"url": PROFILE})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    data = response.json()
    assert (data["type"], data["handle"], data["name"], data["url"]) == ("profile", "scout2015", "Scout", PROFILE)
    assert (data["more"], data["warning"]) == (False, None)
    assert [p["id"] for p in data["posts"]] == [post_id(n) for n in (5, 4, 3, 2, 1)]  # newest first
    newest = data["posts"][0]
    assert newest == {
        "id": post_id(5),
        "url": f"https://www.tiktok.com/@scout2015/video/{post_id(5)}",
        "caption": "caption of post 5 #tag5",
        "date": "2023-11-19",
        "duration": 15,
        "thumbnail": "https://cdn.example.test/5.jpg",
        "views": 5000,
        "likes": 50,
    }
    assert data["bulk"] == {"enabled": True, "max_posts": 30, "delay": 0.0, "audio": True}
    assert backend.list_calls == [("scout2015", 90)]


def test_a_profile_is_found_inside_pasted_text_and_cached(client) -> None:
    c, backend, _ = client
    for text in (f"follow me {PROFILE}?lang=en", "tiktok.com/@Scout2015"):
        assert c.post("/api/info", json={"url": text}).json()["type"] == "profile"
    assert len(backend.list_calls) == 1  # "scout2015" and "Scout2015" differ only in case: one cache entry
    assert c.post("/api/info", json={"url": PROFILE}).status_code == 200
    assert len(backend.list_calls) == 1


def test_the_list_is_limited_and_says_there_is_more() -> None:
    c, backend, _ = make(profile_list_limit=3)
    with c:
        data = c.post("/api/info", json={"url": PROFILE}).json()
    assert [p["id"] for p in data["posts"]] == [post_id(n) for n in (5, 4, 3)]
    assert data["more"] is True and backend.list_calls == [("scout2015", 3)]


def test_a_listing_cut_short_by_tiktok_carries_its_warning() -> None:
    backend = FakeBackend(listing=make_listing(3, more=True, warning="TikTok stopped the list early. Try later."))
    c, _, _ = make(backend)
    with c:
        data = c.post("/api/info", json={"url": PROFILE}).json()
    assert data["more"] is True and data["warning"] == "TikTok stopped the list early. Try later."


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [(LoginRequired("This account is private, so its posts can't be listed."), 403, "login_required"),
     (VideoUnavailable("This account hasn't posted any public videos."), 404, "video_unavailable"),
     (UpstreamBlocked(), 502, "upstream_blocked")],
)  # fmt: skip
def test_profile_errors_are_friendly_json(error, status: int, code: str) -> None:
    c, _, _ = make(FakeBackend(error=error))
    with c:
        response = c.post("/api/info", json={"url": PROFILE})
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert response.json()["error"]["message"] == error.message


def test_single_posts_say_what_they_are(client) -> None:
    c, _, _ = client
    data = c.post("/api/info", json={"url": f"{PROFILE}/video/{post_id(1)}"}).json()
    assert data["type"] == "post" and data["downloads"]


def test_a_short_link_to_a_profile_lists_the_profile() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": f"{PROFILE}?_d=abc"})

    c, backend, service = make()
    service.http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    with c:
        assert c.post("/api/info", json={"url": "https://vm.tiktok.com/ZMabcdef/"}).json()["type"] == "profile"
    assert backend.list_calls == [("scout2015", 90)]


def test_a_profile_link_is_not_a_download(client) -> None:
    c, backend, _ = client
    response = c.get("/api/download", params={"url": PROFILE, "kind": "video"})
    assert response.status_code == 400 and "profile" in response.json()["error"]["message"]
    assert backend.open_calls == []


# ------------------------------------------------------------------------- the ZIP


def test_a_bulk_request_returns_one_zip_with_everything_and_a_manifest(client) -> None:
    c, backend, _ = client
    response = bulk(c, [3, 2, 1], what="media,audio")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["content-disposition"] == 'attachment; filename="tiktok_scout2015_3_posts.zip"'
    assert response.headers["cache-control"] == "no-store" and f"dl_{JOB}=1" in response.headers["set-cookie"]

    listed = names(response)
    root = "tiktok_scout2015/"
    assert all(n.startswith(root) for n in listed)
    expected = [f"{root}undated_{post_id(n)}_post-{post_id(n)}{suffix}" for n in (3, 2, 1) for suffix in (".mp4", "_sound.mp3")]
    assert listed == [*expected, f"{root}posts.json", f"{root}posts.csv", f"{root}REPORT.txt"]

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.read(expected[0]) == backend.body(post_id(3), "video")
        assert archive.read(expected[1]) == backend.body(post_id(3), "audio")
        manifest = json.loads(archive.read(f"{root}posts.json"))
    assert {m["id"] for m in manifest} == {post_id(n) for n in (1, 2, 3)} and all(m["status"] == "ok" for m in manifest)
    assert backend.lookups == [post_id(n) for n in (3, 2, 1)]  # one lookup per post, in the order asked


def test_progress_is_readable_afterwards_and_the_slot_is_free_again(client) -> None:
    c, _, service = client
    assert bulk(c, [1, 2]).status_code == 200
    progress = c.get("/api/bulk/progress", params={"n": JOB})
    assert progress.status_code == 200 and progress.headers["cache-control"] == "no-store"
    snap = progress.json()
    assert (snap["state"], snap["total"], snap["done"], snap["saved"]) == ("done", 2, 2, 2)
    assert service._bulk_gate.acquire()  # released at the end of the run
    service._bulk_gate.release()
    assert bulk(c, [3], job="zyxwvutsrq9876543210").status_code == 200  # so another one can start


def test_posts_that_cannot_be_saved_are_reported_inside_the_zip_and_in_progress(client) -> None:
    backend = FakeBackend(failures={(post_id(2), "lookup"): [VideoUnavailable()]}, listing=make_listing(3))
    c, _, _ = make(backend)
    with c:
        response = bulk(c, [3, 2, 1])
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            report = archive.read("tiktok_scout2015/REPORT.txt").decode()
        snap = c.get("/api/bulk/progress", params={"n": JOB}).json()
    assert "Saved 2 of 3 posts." in report and f"{post_id(2)}:" in report
    assert (snap["saved"], snap["skipped"], snap["state"]) == (2, 1, "done")


def test_pressing_stop_finishes_the_zip_with_what_is_done() -> None:
    holder: dict[str, Any] = {}

    def on_lookup(pid: str) -> None:
        if pid == post_id(4):  # the user presses "stop" while the 2nd post (id ...4) is being fetched
            holder["client"].post("/api/bulk/stop", params={"n": JOB})

    backend = FakeBackend(listing=make_listing(5), on_lookup=on_lookup)
    c, _, _ = make(backend)
    holder["client"] = c
    with c:
        response = bulk(c, [5, 4, 3, 2, 1])
        listed = names(response)
        snap = c.get("/api/bulk/progress", params={"n": JOB}).json()
    assert backend.lookups == [post_id(5), post_id(4)]  # post 4 finished, then it stopped
    assert [n for n in listed if n.endswith(".mp4")] == [f"tiktok_scout2015/undated_{post_id(n)}_post-{post_id(n)}.mp4" for n in (5, 4)]
    assert "tiktok_scout2015/posts.json" in listed  # a complete, valid archive
    assert (snap["state"], snap["saved"], snap["message"]) == ("stopped", 2, "Stopped on request.")


def test_repeated_blocking_ends_the_zip_early_with_a_report() -> None:
    backend = FakeBackend(failures={(post_id(2), "lookup"): [UpstreamBlocked()] * 10}, listing=make_listing(4))
    c, _, _ = make(backend, bulk_max_backoff=1)
    with c:
        response = bulk(c, [4, 3, 2, 1])
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            report = archive.read("tiktok_scout2015/REPORT.txt").decode()
        snap = c.get("/api/bulk/progress", params={"n": JOB}).json()
    assert snap["state"] == "aborted" and "refusing requests" in report
    assert post_id(1) not in backend.lookups  # it stopped asking TikTok


# ---------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "overrides",
    [{"user": "../etc"}, {"user": "a b"}, {"user": "x" * 101}, {"ids": ""}, {"ids": "abc"}, {"ids": "123"},
     {"ids": f"{post_id(1)},not-a-number"}, {"what": ""}, {"what": "video"}, {"what": "media,exe"}, {"job": "short"},
     {"job": "has spaces in it 123456"}],
)  # fmt: skip
def test_bad_bulk_requests_are_rejected_before_anything_starts(client, overrides: dict[str, str]) -> None:
    c, backend, service = client
    params = {"user": "scout2015", "ids": post_id(1), "what": "media", "n": JOB}
    params.update({"n": overrides["job"]} if "job" in overrides else overrides)
    response = c.get("/api/bulk", params=params)
    assert response.status_code == 400 and response.json()["error"]["code"] in {"invalid_request", "invalid_url"}
    assert backend.lookups == []
    assert service._bulk_gate.acquire()  # a rejected request does not keep the slot
    service._bulk_gate.release()


def test_too_many_posts_for_one_zip_is_refused() -> None:
    c, backend, _ = make(bulk_max_posts=3)
    with c:
        response = bulk(c, [1, 2, 3, 4])
        assert response.status_code == 400 and "between 1 and 3" in response.json()["error"]["message"]
        assert bulk(c, [1, 2, 2, 1, 3]).status_code == 200  # duplicates are folded away first
    assert backend.lookups == [post_id(1), post_id(2), post_id(3)]


def test_bulk_can_be_turned_off() -> None:
    c, backend, _ = make(bulk_enabled=False)
    with c:
        assert c.post("/api/info", json={"url": PROFILE}).json()["bulk"]["enabled"] is False  # the page hides the controls
        response = bulk(c, [1])
    assert response.status_code == 403 and response.json()["error"]["code"] == "disabled"
    assert backend.lookups == []


def test_only_one_bulk_download_runs_at_a_time(client) -> None:
    c, backend, service = client
    assert service._bulk_gate.acquire()  # someone else's run
    busy = bulk(c, [1])
    assert busy.status_code == 503 and busy.headers["retry-after"] == "30" and busy.json()["error"]["code"] == "busy"
    service._bulk_gate.release()
    assert bulk(c, [1]).status_code == 200
    assert backend.lookups == [post_id(1)]


def test_a_download_id_cannot_be_reused_while_it_is_running(client) -> None:
    c, _, service = client
    service._track(JOB, __import__("app.archive", fromlist=["Progress"]).Progress(1))  # a run that has not finished
    response = bulk(c, [1])
    assert response.status_code == 400 and "already running" in response.json()["error"]["message"]
    assert service._bulk_gate.acquire()  # and the failed attempt gave its slot back
    service._bulk_gate.release()


def test_bulk_starts_are_rate_limited_per_client() -> None:
    c, _, _ = make(rate_limit_bulk=1)
    with c:
        assert bulk(c, [1]).status_code == 200
        limited = bulk(c, [2], job="zyxwvutsrq9876543210")
    assert limited.status_code == 429 and 1 <= int(limited.headers["retry-after"]) <= 3600


def test_unknown_downloads_have_no_progress_and_cannot_be_stopped(client) -> None:
    c, _, _ = client
    for response in (c.get("/api/bulk/progress", params={"n": "nothing-here-at-all"}),
                     c.post("/api/bulk/stop", params={"n": "nothing-here-at-all"})):  # fmt: skip
        assert response.status_code == 404 and response.json()["error"]["code"] == "unknown_job"


# ------------------------------------------------------------ real network stack


@pytest.fixture
def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_a_client_that_leaves_mid_zip_frees_the_slot_and_stops_asking_tiktok(free_port: int) -> None:
    """Uses a real server: TestClient cannot simulate a dropped connection."""
    backend = FakeBackend(listing=make_listing(20), on_lookup=lambda pid: time.sleep(0.15))
    app = create_app(settings(), backend)
    service = app.state.service
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=free_port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started
        ids = ",".join(post_id(n) for n in range(1, 21))
        with (
            httpx.Client(timeout=10, trust_env=False) as http,
            http.stream("GET", f"http://127.0.0.1:{free_port}/api/bulk",
                        params={"user": "scout2015", "ids": ids, "what": "media", "n": JOB}) as response,
        ):  # fmt: skip
            assert response.status_code == 200
            assert next(response.iter_raw())  # whatever the first network read returns: no buffering to a fixed size
        # leaving the block closed the connection with 19 posts still to go
        deadline = time.time() + 10
        while service._bulk_gate.acquire() is False and time.time() < deadline:
            time.sleep(0.05)
        else:
            service._bulk_gate.release()
        assert len(backend.lookups) < 20, "it kept fetching posts for a client that had gone"
        snap = service.bulk_progress(JOB)
        assert snap["state"] == "stopped"
        assert backend.sessions_opened == backend.sessions_closed
    finally:
        server.should_exit = True
        thread.join(timeout=10)
