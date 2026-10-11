"""The bulk-saving loop: naming, manifest, pacing and backoff, cancellation and cleanup.

Nothing here sleeps: the pacer's ``wait`` is a recorder, so back-off schedules can be asserted exactly.
"""
from __future__ import annotations

import csv
import io
import json
import random
import threading
from typing import Any

import pytest

from app.archive import (
    CSV_COLUMNS,
    ArchiveFile,
    Pacer,
    PostRef,
    Progress,
    Wants,
    archive_posts,
    manifest_csv,
    post_stem,
    report_text,
    slugify,
    spool,
)
from app.backend import Download
from app.errors import (
    ConversionFailed,
    FormatUnavailable,
    InvalidRequest,
    LoginRequired,
    TooLarge,
    UpstreamBlocked,
    UpstreamError,
    VideoUnavailable,
)
from tests.fakes import BASE_TIME, FakeBackend, make_info, make_post_info, post_id, post_url

MAX = 50 * 1024 * 1024


class Waits:
    """A ``wait`` that records what it was asked to sleep and can pretend the run was cancelled."""

    def __init__(self, cancel_on_call: int | None = None, progress: Progress | None = None) -> None:
        self.calls: list[float] = []
        self.cancel_on_call = cancel_on_call
        self.progress = progress

    def __call__(self, seconds: float) -> bool:
        self.calls.append(seconds)
        if self.cancel_on_call is not None and len(self.calls) >= self.cancel_on_call:
            if self.progress:
                self.progress.cancel.set()
            return True
        return False


def refs(*numbers: int) -> list[PostRef]:
    return [PostRef(post_id(n), post_url(n)) for n in numbers]


def pacer(waits: Waits | None = None, **kwargs: Any) -> Pacer:
    return Pacer(waits or Waits(), rng=random.Random(7), **kwargs)


def run(backend: FakeBackend, posts: list[PostRef], wants: Wants | None = None, **kwargs: Any) -> tuple[dict[str, bytes], list[str], Progress]:
    """Drain the generator the way a consumer would: read each file fully and close it."""
    wants = wants or Wants()
    progress = kwargs.pop("progress", None) or Progress(len(posts))
    files: dict[str, bytes] = {}
    order: list[str] = []
    for item in archive_posts(backend, posts, wants, pacer=kwargs.pop("pacer", None) or pacer(), progress=progress,
                              max_bytes=kwargs.pop("max_bytes", MAX), **kwargs):  # fmt: skip
        try:
            files[item.path] = item.data.read()
            order.append(item.path)
        finally:
            item.close()
    return files, order, progress


def records(files: dict[str, bytes]) -> list[dict[str, Any]]:
    return json.loads(files["posts.json"])


# ------------------------------------------------------------------------ naming


@pytest.mark.parametrize(
    ("text", "slug"),
    [
        ("POV: you finally figured it out!! #fyp", "pov-you-finally-figured-it-out-fyp"),
        ("Café déjà vu", "cafe-deja-vu"),
        ("名前 😀 日本語", ""),  # nothing ASCII survives
        ("---", ""),
        ("a" * 100, "a" * 40),
        ("word " * 20, "word-word-word-word-word-word-word-word"),  # cut at the limit without a trailing dash
        ("../../etc/passwd", "etc-passwd"),
    ],
)
def test_slugs_are_ascii_and_short(text: str, slug: str) -> None:
    assert slugify(text) == slug


def test_the_file_stem_sorts_by_date_and_stays_unique_by_id() -> None:
    info = make_post_info(3)
    assert post_stem(info, PostRef(post_id(3), post_url(3))) == f"2023-11-17_{post_id(3)}_caption-of-post-3-tag3"
    undated = make_info(id=post_id(9), timestamp=None, caption="", description="", title="")
    assert post_stem(undated, PostRef(post_id(9), post_url(9))) == f"undated_{post_id(9)}"
    emoji_only = make_post_info(4, caption="😀", description="😀", title="😀")
    assert post_stem(emoji_only, PostRef(post_id(4), post_url(4))) == f"2023-11-18_{post_id(4)}"


@pytest.mark.parametrize(
    ("text", "wants"),
    [
        ("media", Wants(True, False, False)),
        ("media,audio,cover", Wants(True, True, True)),
        (" audio , cover ", Wants(False, True, True)),
        ("cover,cover", Wants(False, False, True)),
    ],
)
def test_wants_parse(text: str, wants: Wants) -> None:
    assert Wants.parse(text) == wants


@pytest.mark.parametrize("text", ["", " , ", "video", "media,rm -rf", "MEDIA"])
def test_wants_rejects_anything_unexpected(text: str) -> None:
    with pytest.raises(InvalidRequest):
        Wants.parse(text)


# ------------------------------------------------------------------------ pacer


def test_the_pause_between_posts_is_varied_around_the_base_delay() -> None:
    p = pacer(delay=4.0, jitter=0.5)
    gaps = [p.gap() for _ in range(200)]
    assert all(2.0 <= g <= 6.0 for g in gaps)
    assert max(gaps) - min(gaps) > 2.0  # not a metronome


@pytest.mark.parametrize("error", [VideoUnavailable(), LoginRequired(), FormatUnavailable(), TooLarge()])
def test_posts_that_are_gone_are_skipped_without_waiting(error: Exception) -> None:
    decision = pacer().decide(error, 0)  # type: ignore[arg-type]
    assert (decision.action, decision.wait, decision.permanent) == ("skip", 0.0, True)


def test_back_off_doubles_each_time_tiktok_pushes_back_then_gives_up() -> None:
    p = pacer(backoff_start=30, backoff_max=900, max_blocks=4)
    waits = []
    for _ in range(4):
        decision = p.decide(UpstreamBlocked(), 0)
        assert decision.action == "retry"
        waits.append(decision.wait)
    for wait, base in zip(waits, (30, 60, 120, 240), strict=True):
        assert base <= wait <= base * 1.25
    final = p.decide(UpstreamBlocked(), 0)
    assert final.action == "abort" and "refusing requests" in final.reason


def test_back_off_is_capped() -> None:
    p = pacer(backoff_start=30, backoff_max=100, max_blocks=10)
    assert [p.decide(UpstreamBlocked(), 0).wait for _ in range(6)][-1] <= 125


def test_a_healthy_answer_resets_the_back_off() -> None:
    p = pacer(backoff_start=30, max_blocks=2)
    p.decide(UpstreamBlocked(), 0)
    p.decide(UpstreamBlocked(), 0)
    p.healthy()
    assert p.decide(UpstreamBlocked(), 0).action == "retry"  # would have been "abort" without the reset


def test_a_post_that_is_simply_gone_counts_as_a_healthy_answer() -> None:
    p = pacer(max_blocks=1)
    p.decide(UpstreamBlocked(), 0)
    p.decide(VideoUnavailable(), 0)
    assert p.decide(UpstreamBlocked(), 0).action == "retry"


def test_hiccups_are_retried_a_couple_of_times_then_skipped() -> None:
    p = pacer(max_retries=2, delay=3.0)
    assert [p.decide(UpstreamError(), n).action for n in (0, 1)] == ["retry", "retry"]
    assert p.decide(UpstreamError(), 2).action == "skip"
    assert p.decide(ConversionFailed(), 0).action == "retry"


def test_many_failures_in_a_row_stop_the_run() -> None:
    p = pacer(max_retries=0, max_failed_in_a_row=3)
    assert [p.decide(UpstreamError(), 0).action for _ in range(3)] == ["skip", "skip", "abort"]


# ----------------------------------------------------------------- the main loop


def test_each_post_is_looked_up_once_and_a_pause_separates_the_posts() -> None:
    backend = FakeBackend()
    waits = Waits()
    _, order, progress = run(backend, refs(3, 2, 1), pacer=pacer(waits, delay=4.0))

    saved = [f"undated_{post_id(n)}_post-{post_id(n)}.mp4" for n in (3, 2, 1)]  # no date known to the fake
    assert order == [*saved, "posts.json", "posts.csv", "REPORT.txt"]
    assert backend.lookups == [post_id(3), post_id(2), post_id(1)]  # one TikTok lookup per post
    assert backend.sessions_opened == backend.sessions_closed == 3
    assert len(waits.calls) == 2 and all(2.0 <= w <= 6.0 for w in waits.calls)  # between posts, never before the first
    snap = progress.snapshot()
    assert (snap["state"], snap["total"], snap["done"], snap["saved"]) == ("done", 3, 3, 3)
    assert (snap["skipped"], snap["failed"], snap["current"]) == (0, 0, None)


def test_files_have_the_right_content_and_the_manifest_describes_them() -> None:
    backend = FakeBackend(posts={post_id(n): make_post_info(n) for n in (1, 2)})
    files, order, _ = run(backend, refs(2, 1))
    video = next(p for p in order if post_id(2) in p)
    assert video == f"2023-11-16_{post_id(2)}_caption-of-post-2-tag2.mp4"
    assert files[video] == backend.body(post_id(2), "video")

    manifest = records(files)
    assert [r["id"] for r in manifest] == [post_id(2), post_id(1)]  # newest first
    first = manifest[0]
    assert first["status"] == "ok" and first["files"] == [video] and first["warnings"] == []
    assert first["caption"] == "caption of post 2 #tag2" and first["hashtags"] == ["tag2"]
    assert first["posted_at"] == "2023-11-16T22:13:20Z" and first["type"] == "video"
    assert first["author"] == {"name": "Scout", "handle": "scout2015"}
    assert first["stats"]["views"] == 1_234_567


def test_sound_and_cover_sit_beside_a_video_and_inside_a_slideshow_folder() -> None:
    video = make_post_info(1)
    slideshow = make_post_info(2, images=[["https://a.test/1.jpg"], ["https://a.test/2.png"], ["https://a.test/3.jpg"]])
    backend = FakeBackend(posts={post_id(1): video, post_id(2): slideshow})
    files, order, _ = run(backend, refs(1, 2), Wants(media=True, audio=True, cover=True))

    v = f"2023-11-15_{post_id(1)}_caption-of-post-1-tag1"
    s = f"2023-11-16_{post_id(2)}_caption-of-post-2-tag2"
    assert [p for p in order if p.startswith(v)] == [f"{v}.mp4", f"{v}_sound.mp3", f"{v}_cover.jpg"]
    assert [p for p in order if p.startswith(s)] == [
        f"{s}/01.jpg", f"{s}/02.png", f"{s}/03.jpg", f"{s}/sound.mp3", f"{s}/cover.jpg",
    ]  # fmt: skip
    assert files[f"{s}/02.png"] == backend.body(post_id(2), "image:1")
    by_id = {r["id"]: r for r in records(files)}
    assert by_id[post_id(2)]["type"] == "photos" and by_id[post_id(2)]["photo_count"] == 3


def test_only_the_chosen_extras_are_fetched() -> None:
    backend = FakeBackend()
    _, order, _ = run(backend, refs(1), Wants(media=False, audio=True, cover=False))
    assert [p for p in order if p not in {"posts.json", "posts.csv", "REPORT.txt"}] == [
        f"undated_{post_id(1)}_post-{post_id(1)}_sound.mp3"
    ]
    assert backend.opened == [(post_id(1), "audio")]


def test_a_failing_extra_costs_a_warning_not_the_post() -> None:
    backend = FakeBackend(failures={(post_id(1), "audio"): [FormatUnavailable("This post has no downloadable sound.")]})
    files, order, progress = run(backend, refs(1), Wants(media=True, audio=True))
    assert any(p.endswith(".mp4") for p in order) and not any(p.endswith(".mp3") for p in order)
    only = records(files)[0]
    assert only["status"] == "ok" and only["warnings"] == ["sound: This post has no downloadable sound."]
    assert progress.snapshot()["saved"] == 1
    assert "Saved with problems" in files["REPORT.txt"].decode()


def test_a_photo_that_fails_is_noted_and_the_rest_are_kept() -> None:
    slideshow = make_post_info(1, images=[["https://a.test/1.jpg"], ["https://a.test/2.jpg"]])
    backend = FakeBackend(posts={post_id(1): slideshow},
                          failures={(post_id(1), "image:0"): [FormatUnavailable("Photo 1 of this post couldn't be downloaded.")]})  # fmt: skip
    files, order, _ = run(backend, refs(1))
    assert [p for p in order if "/" in p] == [f"2023-11-15_{post_id(1)}_caption-of-post-1-tag1/02.png"]
    assert records(files)[0]["warnings"] == ["photo 1: Photo 1 of this post couldn't be downloaded."]


def test_a_slideshow_whose_photos_all_fail_is_a_failed_post() -> None:
    slideshow = make_post_info(1, images=[["https://a.test/1.jpg"]])
    backend = FakeBackend(posts={post_id(1): slideshow},
                          failures={(post_id(1), "image:0"): [FormatUnavailable("nope")]})  # fmt: skip
    files, order, progress = run(backend, refs(1))
    assert order == ["posts.json", "posts.csv", "REPORT.txt"]
    only = records(files)[0]
    assert only["status"] == "skipped" and "photos could be downloaded" in only["reason"]
    assert progress.snapshot()["skipped"] == 1


def test_posts_that_are_gone_are_skipped_and_the_run_carries_on() -> None:
    backend = FakeBackend(failures={(post_id(2), "lookup"): [VideoUnavailable()], (post_id(3), "video"): [LoginRequired()]})
    waits = Waits()
    files, order, progress = run(backend, refs(1, 2, 3, 4), pacer=pacer(waits))
    statuses = {r["id"]: r["status"] for r in records(files)}
    assert statuses == {post_id(1): "ok", post_id(2): "skipped", post_id(3): "skipped", post_id(4): "ok"}
    assert len(waits.calls) == 3  # only the normal pauses: skipping is not retried or backed off
    assert backend.lookups == [post_id(n) for n in (1, 2, 3, 4)]
    snap = progress.snapshot()
    assert (snap["state"], snap["saved"], snap["skipped"], snap["done"]) == ("done", 2, 2, 4)
    assert backend.sessions_opened == backend.sessions_closed  # sessions are closed even when a post fails


def test_a_hiccup_is_retried_after_a_short_wait_and_then_succeeds() -> None:
    backend = FakeBackend(failures={(post_id(1), "lookup"): [UpstreamError()]})
    waits = Waits()
    files, _, _ = run(backend, refs(1), pacer=pacer(waits, delay=3.0))
    assert records(files)[0]["status"] == "ok"
    assert backend.lookups == [post_id(1), post_id(1)]  # looked up twice
    assert len(waits.calls) == 1 and 6.0 <= waits.calls[0] <= 9.0  # delay * 2 * [1, 1.5]


def test_when_tiktok_pushes_back_the_run_backs_off_then_recovers() -> None:
    backend = FakeBackend(failures={(post_id(1), "lookup"): [UpstreamBlocked(), UpstreamBlocked()]})
    waits = Waits()
    files, _, progress = run(backend, refs(1, 2), pacer=pacer(waits, delay=3.0, backoff_start=30))
    assert [r["status"] for r in records(files)] == ["ok", "ok"]
    first, second, between = waits.calls
    assert 30 <= first <= 37.5 and 60 <= second <= 75  # exponential
    assert 1.5 <= between <= 4.5  # then the normal pause before post 2
    assert progress.snapshot()["state"] == "done"


def test_repeated_blocking_aborts_the_run_with_a_clear_report_and_no_more_requests() -> None:
    backend = FakeBackend(failures={(post_id(2), "lookup"): [UpstreamBlocked()] * 10})
    waits = Waits()
    files, order, progress = run(backend, refs(1, 2, 3, 4), pacer=pacer(waits, backoff_start=10, max_blocks=2))
    snap = progress.snapshot()
    assert (snap["state"], snap["saved"], snap["failed"]) == ("aborted", 1, 1)
    assert backend.lookups.count(post_id(2)) == 3  # the first try plus two back-offs
    assert post_id(3) not in backend.lookups and post_id(4) not in backend.lookups  # it stopped asking
    report = files["REPORT.txt"].decode()
    assert "ended early" in report and "refusing requests" in report and "Saved 1 of 2 posts" in report
    assert {r["id"]: r["status"] for r in records(files)} == {post_id(1): "ok", post_id(2): "failed"}


def test_a_string_of_unexplained_failures_aborts_instead_of_hammering() -> None:
    backend = FakeBackend(failures={(post_id(n), "lookup"): [UpstreamError()] * 5 for n in range(1, 9)})
    _, _, progress = run(backend, refs(*range(1, 9)), pacer=pacer(max_retries=0, max_failed_in_a_row=3))
    assert progress.snapshot()["state"] == "aborted"
    assert len(backend.lookups) == 3


def test_cancelling_during_a_pause_ends_the_run_but_still_delivers_the_manifest() -> None:
    backend = FakeBackend()
    progress = Progress(4)
    waits = Waits(cancel_on_call=2, progress=progress)  # the 2nd pause is interrupted
    files, order, _ = run(backend, refs(1, 2, 3, 4), progress=progress, pacer=pacer(waits))
    assert backend.lookups == [post_id(1), post_id(2)]
    snap = progress.snapshot()
    assert (snap["state"], snap["saved"]) == ("stopped", 2) and snap["message"] == "Stopped on request."
    assert "ended early" in files["REPORT.txt"].decode() and len(records(files)) == 2


def test_cancelling_before_the_run_starts_saves_nothing() -> None:
    progress = Progress(2)
    progress.cancel.set()
    files, order, _ = run(FakeBackend(), refs(1, 2), progress=progress)
    assert order == ["posts.json", "posts.csv", "REPORT.txt"] and records(files) == []
    assert progress.snapshot()["state"] == "stopped"


def test_the_time_limit_stops_the_run() -> None:
    now = {"t": 0.0}

    def clock() -> float:
        now["t"] += 10
        return now["t"]

    backend = FakeBackend()
    _, _, progress = run(backend, refs(1, 2, 3, 4, 5), deadline=25.0, clock=clock)
    assert progress.snapshot()["state"] == "stopped" and 0 < len(backend.lookups) < 5


def test_a_consumer_that_walks_away_releases_everything() -> None:
    backend = FakeBackend()
    progress = Progress(3)
    generator = archive_posts(backend, refs(1, 2, 3), Wants(media=True, audio=True), pacer=pacer(), progress=progress, max_bytes=MAX)
    first = next(generator)  # a file is in flight, its session still open
    first.close()
    generator.close()  # the client disconnected
    assert backend.sessions_opened == backend.sessions_closed == 1
    assert backend.lookups == [post_id(1)]
    snap = progress.snapshot()
    assert snap["state"] == "stopped" and snap["message"] == "The connection was closed."
    assert backend.closed == len(backend.opened)  # every download that was opened was closed


def test_files_not_yet_consumed_are_closed_when_the_consumer_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    closes: list[str] = []
    original = ArchiveFile.close
    monkeypatch.setattr(ArchiveFile, "close", lambda self: (closes.append(self.path), original(self))[1])

    backend = FakeBackend()
    generator = archive_posts(backend, refs(1), Wants(media=True, audio=True, cover=True), pacer=pacer(),
                              progress=Progress(1), max_bytes=MAX)  # fmt: skip
    next(generator)  # video, sound and cover were all fetched; only the first was handed over
    assert closes == []
    generator.close()  # the consumer walked away without closing anything
    assert len(closes) == 3 and len(set(closes)) == 3  # all three files were closed, none twice over


def test_records_from_earlier_runs_are_merged_and_each_new_record_is_reported() -> None:
    old = {"id": post_id(9), "url": post_url(9), "status": "ok", "posted_at": "2020-01-01T00:00:00Z", "files": ["old.mp4"]}
    backend = FakeBackend(posts={post_id(1): make_post_info(1)})
    seen: list[str] = []
    files, _, _ = run(backend, refs(1), known=[old], on_record=lambda r: seen.append(r["id"]))
    assert [r["id"] for r in records(files)] == [post_id(1), post_id(9)]  # newest first, old one kept
    assert seen == [post_id(1)]  # the callback only sees what this run did


def test_a_file_over_the_size_cap_is_skipped() -> None:
    backend = FakeBackend()
    files, _, _ = run(backend, refs(1), max_bytes=100)
    only = records(files)[0]
    assert only["status"] == "skipped" and "too large" in only["reason"]


def test_a_transfer_that_ends_early_never_becomes_a_file() -> None:
    backend = FakeBackend(short_reads={(post_id(1), "video")})
    files, order, _ = run(backend, refs(1), pacer=pacer(max_retries=0))
    assert not any(p.endswith(".mp4") for p in order)
    assert records(files)[0]["status"] == "failed" and "cut the transfer short" in records(files)[0]["reason"]


# ------------------------------------------------------------------------ spooling


def download_of(data: bytes, *, length: int | None) -> Download:
    stream = io.BytesIO(data)
    return Download(filename="f.bin", media_type="x/y", length=length, reader=stream.read)


def test_spool_returns_a_rewound_file_and_closes_the_download() -> None:
    download = download_of(b"abc" * 1000, length=3000)
    file = spool(download, "a/b.bin", mtime=5, max_bytes=MAX, temp_dir=None)
    assert (file.path, file.size, file.mtime) == ("a/b.bin", 3000, 5)
    assert file.data.read() == b"abc" * 1000
    assert download._closed
    file.close()


def test_large_files_spill_to_disk_but_arrive_intact() -> None:
    payload = bytes(range(256)) * (10 * 4096)  # about 10 MB, past the in-memory limit
    file = spool(download_of(payload, length=len(payload)), "big.bin", mtime=None, max_bytes=len(payload) + 1, temp_dir=None)
    assert file.size == len(payload) and file.data.read() == payload
    file.close()


def test_spool_enforces_the_cap_even_without_a_content_length() -> None:
    with pytest.raises(TooLarge):
        spool(download_of(b"x" * 5000, length=None), "a", mtime=None, max_bytes=1000, temp_dir=None)


def test_spool_rejects_a_short_transfer() -> None:
    with pytest.raises(UpstreamError, match="cut the transfer short"):
        spool(download_of(b"x" * 10, length=20), "a", mtime=None, max_bytes=MAX, temp_dir=None)


# ----------------------------------------------------------------------- manifest


def read_csv(payload: bytes) -> list[list[str]]:
    assert payload.startswith(b"\xef\xbb\xbf")  # the BOM makes Excel read non-Latin captions correctly
    return list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))


def test_the_csv_survives_commas_quotes_newlines_and_emoji() -> None:
    record = {
        "id": post_id(1), "url": post_url(1), "status": "ok", "type": "video", "posted_at": "2023-11-15T22:13:20Z",
        "caption": 'He said, "wow"\nsecond line 😀 日本語', "hashtags": ["a", "b"], "author": {"handle": "scout2015"},
        "duration": 15, "stats": {"views": 1, "likes": 2, "comments": 3, "shares": 4},
        "sound": {"title": "original sound", "artists": ["Scout", "Friend"]}, "photo_count": None,
        "files": ["a.mp4", "a_sound.mp3"], "warnings": ["sound: slow"],
    }  # fmt: skip
    header, row = read_csv(manifest_csv([record]))
    assert header == CSV_COLUMNS
    cells = dict(zip(header, row, strict=True))
    assert cells["caption"] == 'He said, "wow"\nsecond line 😀 日本語'
    assert (cells["hashtags"], cells["author"], cells["views"], cells["comments"]) == ("a b", "scout2015", "1", "3")
    assert cells["sound"] == "original sound – Scout, Friend" and cells["files"] == "a.mp4; a_sound.mp3"
    assert cells["note"] == "sound: slow"


def test_failed_posts_appear_in_the_csv_with_the_reason() -> None:
    _, row = read_csv(manifest_csv([{"id": "1", "url": "u", "status": "skipped", "reason": "private"}]))
    cells = dict(zip(CSV_COLUMNS, row, strict=True))
    assert (cells["status"], cells["note"], cells["caption"]) == ("skipped", "private", "")


def test_the_report_explains_what_happened() -> None:
    text = report_text(
        [{"id": "1", "status": "ok"}, {"id": "2", "status": "skipped", "reason": "private"},
         {"id": "3", "status": "ok", "warnings": ["sound: slow"]}], "aborted", "TikTok kept refusing.")  # fmt: skip
    assert "Saved 2 of 3 posts." in text
    assert "The run ended early: TikTok kept refusing." in text
    assert "  2: private" in text and "  3: sound: slow" in text
    assert "ended early" not in report_text([{"id": "1", "status": "ok"}], "done", "")


def test_an_empty_run_still_produces_a_valid_manifest() -> None:
    files, _, _ = run(FakeBackend(), [])
    assert records(files) == [] and files["posts.csv"].startswith(b"\xef\xbb\xbfid,url")


def test_progress_snapshots_are_copies_and_threadsafe_to_read() -> None:
    progress = Progress(2)
    snap = progress.snapshot()
    progress.begin("1")
    assert snap["current"] is None and progress.snapshot()["current"] == "1"
    done = threading.Event()

    def reader() -> None:
        while not done.is_set():
            progress.snapshot()

    thread = threading.Thread(target=reader)
    thread.start()
    for _ in range(500):
        progress.begin("x")
        progress.completed("ok")
    done.set()
    thread.join()
    assert progress.snapshot()["saved"] == 500


def test_the_time_stamps_in_names_use_utc_dates() -> None:
    # BASE_TIME is 2023-11-14 22:13:20 UTC: a local-time conversion east of UTC would roll the date over.
    info = make_info(id=post_id(1), timestamp=BASE_TIME, caption="x", description="x", title="x")
    assert post_stem(info, PostRef(post_id(1), post_url(1))).startswith("2023-11-14_")

