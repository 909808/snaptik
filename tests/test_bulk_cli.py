"""The command-line archiver: resuming, pacing, back-off, interruption and path safety. Nothing really sleeps."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.archive import ArchiveFile
from app.bulk import EXIT_BLOCKED, EXIT_FAILED, EXIT_INTERRUPTED, EXIT_OK, load_log, main, plan, write_file
from app.errors import UpstreamBlocked, UpstreamError, VideoUnavailable
from tests.fakes import BASE_TIME, FakeBackend, make_listing, make_post_info, post_id


def backend_for(count: int = 3, **kwargs: Any) -> FakeBackend:
    posts = {post_id(n): make_post_info(n) for n in range(1, count + 1)}
    return FakeBackend(posts=posts, listing=make_listing(count), **kwargs)


class Cli:
    def __init__(self, tmp_path: Path) -> None:
        self.folder = tmp_path / "out"
        self.sleeps: list[float] = []

    def run(self, backend: FakeBackend, *args: str, sleep: Any = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        argv = ["@scout2015", "--out", str(self.folder), *args]
        code = main(argv, backend=backend, sleep=sleep or self.sleeps.append, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def files(self) -> list[str]:
        return sorted(p.relative_to(self.folder).as_posix() for p in self.folder.rglob("*") if p.is_file())

    def log(self) -> dict[str, dict[str, Any]]:
        return load_log(self.folder / "posts.jsonl")


@pytest.fixture
def cli(tmp_path: Path) -> Cli:
    return Cli(tmp_path)


STEMS = {n: f"2023-11-{14 + n}_{post_id(n)}_caption-of-post-{n}-tag{n}" for n in (1, 2, 3)}


# ------------------------------------------------------------------- a full run


def test_a_run_saves_every_post_a_manifest_and_a_log(cli: Cli) -> None:
    backend = backend_for(3)
    code, out, err = cli.run(backend)
    assert (code, err) == (EXIT_OK, "")
    assert cli.files() == sorted([
        *(f"{STEMS[n]}.mp4" for n in (1, 2, 3)), "posts.json", "posts.csv", "posts.jsonl", "REPORT.txt",
    ])  # fmt: skip
    assert (cli.folder / f"{STEMS[3]}.mp4").read_bytes() == backend.body(post_id(3), "video")
    assert set(cli.log()) == {post_id(n) for n in (1, 2, 3)} and all(r["status"] == "ok" for r in cli.log().values())
    manifest = json.loads((cli.folder / "posts.json").read_text(encoding="utf-8"))
    assert [m["id"] for m in manifest] == [post_id(n) for n in (3, 2, 1)]  # newest first
    assert manifest[0]["caption"] == "caption of post 3 #tag3"
    assert "3 posts listed (2023-11-15 to 2023-11-17)" in out and "3 to fetch" in out
    assert "[   1/3]" in out and "[   3/3]" in out and "Saved 3 of 3" in out


def test_files_carry_the_date_the_post_was_published(cli: Cli) -> None:
    cli.run(backend_for(2))
    stamp = (cli.folder / f"{STEMS[2]}.mp4").stat().st_mtime
    assert stamp == BASE_TIME + 2 * 86_400


def test_posts_are_fetched_politely_one_lookup_each_with_a_pause_between(cli: Cli) -> None:
    backend = backend_for(3)
    cli.run(backend, "--delay", "4")
    assert backend.lookups == [post_id(n) for n in (3, 2, 1)]
    assert len(cli.sleeps) == 2 and all(2.0 <= s <= 6.0 for s in cli.sleeps)


def test_the_pause_has_a_floor(cli: Cli) -> None:
    _, out, _ = cli.run(backend_for(2), "--delay", "0.1")
    assert "Using the minimum pause of 1 second between posts" in out and all(0.5 <= s <= 1.5 for s in cli.sleeps)


def test_extras_are_saved_when_asked_for(cli: Cli) -> None:
    cli.run(backend_for(1), "--what", "media,audio,cover")
    stem = STEMS[1]
    assert {f"{stem}.mp4", f"{stem}_sound.mp3", f"{stem}_cover.jpg"} <= set(cli.files())


def test_a_caption_the_console_cannot_show_does_not_end_the_run(cli: Cli) -> None:
    """Emoji in a caption on a console with a legacy code page (Windows, output piped to a file)."""
    posts = {post_id(1): make_post_info(1, caption="so fun 😀 日本語 #fyp", description="so fun 😀 日本語 #fyp")}
    backend = FakeBackend(posts=posts, listing=make_listing(1))
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
    with pytest.raises(UnicodeEncodeError):
        console.write("😀")  # what would have happened to every `print` of such a caption
    code = main(["@scout2015", "--out", str(cli.folder)], backend=backend, sleep=cli.sleeps.append,
                out=console, err=io.StringIO())  # fmt: skip
    assert code == EXIT_OK and "so fun" in raw.getvalue().decode("cp1252")
    assert json.loads((cli.folder / "posts.json").read_text(encoding="utf-8"))[0]["caption"] == "so fun 😀 日本語 #fyp"


def test_a_dry_run_only_reports(cli: Cli) -> None:
    backend = backend_for(3)
    code, out, _ = cli.run(backend, "--dry-run")
    assert code == EXIT_OK and "3 to fetch" in out and "Roughly" in out
    assert backend.lookups == [] and not cli.folder.exists()  # not even an empty output folder


# ------------------------------------------------------------------- resuming


def test_running_again_skips_what_is_saved(cli: Cli) -> None:
    backend = backend_for(3)
    cli.run(backend)
    lookups = list(backend.lookups)
    before = (cli.folder / "posts.json").read_text(encoding="utf-8")
    code, out, _ = cli.run(backend)
    assert code == EXIT_OK and "Nothing to do." in out
    assert backend.lookups == lookups  # TikTok was not asked about any post again
    assert (cli.folder / "posts.json").read_text(encoding="utf-8") == before


def test_a_limited_run_can_be_continued_with_a_full_one(cli: Cli) -> None:
    backend = backend_for(3)
    cli.run(backend, "--limit", "2")  # the newest two: posts 3 and 2
    assert backend.lookups == [post_id(3), post_id(2)]
    cli.run(backend)
    assert backend.lookups == [post_id(3), post_id(2), post_id(1)]  # only the missing one was fetched
    manifest = json.loads((cli.folder / "posts.json").read_text(encoding="utf-8"))
    assert [m["id"] for m in manifest] == [post_id(n) for n in (3, 2, 1)]  # the manifest covers every run
    assert len([f for f in cli.files() if f.endswith(".mp4")]) == 3


def test_failures_are_retried_on_the_next_run_but_unavailable_posts_are_not(cli: Cli) -> None:
    backend = backend_for(3, failures={
        (post_id(2), "lookup"): [VideoUnavailable()],  # gone for good
        (post_id(1), "lookup"): [UpstreamError()] * 3,  # TikTok misbehaving: first try plus two retries
    })  # fmt: skip
    code, out, _ = cli.run(backend)
    assert code == EXIT_OK
    assert {pid: r["status"] for pid, r in cli.log().items()} == {post_id(3): "ok", post_id(2): "skipped", post_id(1): "failed"}
    assert "1 unavailable" in out and "1 failed" in out

    backend.lookups.clear()
    cli.run(backend)
    assert backend.lookups == [post_id(1)]  # only the failed one: the removed one is settled
    assert cli.log()[post_id(1)]["status"] == "ok" and cli.log()[post_id(2)]["status"] == "skipped"


def test_retry_skipped_looks_at_unavailable_posts_again(cli: Cli) -> None:
    backend = backend_for(2, failures={(post_id(1), "lookup"): [VideoUnavailable()]})
    cli.run(backend)
    assert cli.log()[post_id(1)]["status"] == "skipped"
    backend.lookups.clear()
    cli.run(backend, "--retry-skipped")
    assert backend.lookups == [post_id(1)] and cli.log()[post_id(1)]["status"] == "ok"


def test_the_log_survives_a_crash_in_the_middle_of_a_line(cli: Cli) -> None:
    cli.run(backend_for(2))
    with (cli.folder / "posts.jsonl").open("a", encoding="utf-8") as log:
        log.write('{"id": "9999999999", "status": "ok", "fil')  # the power went out mid-write
    assert set(cli.log()) == {post_id(1), post_id(2)}
    code, out, _ = cli.run(backend_for(2))
    assert code == EXIT_OK and "Nothing to do." in out


# --------------------------------------------------------- pushing back and stopping


def test_when_tiktok_keeps_refusing_it_backs_off_for_a_long_time_then_stops_cleanly(cli: Cli) -> None:
    backend = backend_for(4, failures={(post_id(3), "lookup"): [UpstreamBlocked()] * 50})  # the 2nd post processed
    code, out, err = cli.run(backend)
    assert code == EXIT_BLOCKED and "Stopped early" in err and "refusing requests" in err
    waits = [s for s in cli.sleeps if s >= 30]
    assert len(waits) == 5
    assert all(a < b for a, b in zip(waits, waits[1:], strict=False))  # each wait longer than the last
    assert 60 <= waits[0] <= 75 and waits[-1] <= 900 * 1.25  # starts at a minute, capped at --max-backoff
    assert "Waiting" in out
    assert set(cli.log()) == {post_id(4), post_id(3)} and cli.log()[post_id(4)]["status"] == "ok"
    assert post_id(2) not in backend.lookups  # it stopped asking instead of hammering


def test_a_blocked_run_carries_on_the_next_time(cli: Cli) -> None:
    backend = backend_for(3, failures={(post_id(2), "lookup"): [UpstreamBlocked()] * 50})
    assert cli.run(backend)[0] == EXIT_BLOCKED
    backend.failures.clear()  # TikTok calmed down
    code, out, _ = cli.run(backend)
    assert code == EXIT_OK and "Saved 2 of 2" in out
    assert {r["status"] for r in cli.log().values()} == {"ok"} and len(cli.log()) == 3


def test_max_backoff_caps_the_waits(cli: Cli) -> None:
    backend = backend_for(2, failures={(post_id(2), "lookup"): [UpstreamBlocked()] * 50})
    cli.run(backend, "--max-backoff", "100")
    assert max(s for s in cli.sleeps if s >= 30) <= 125


def test_ctrl_c_stops_cleanly_and_leaves_no_half_files(cli: Cli) -> None:
    backend = backend_for(3)

    def interrupt(seconds: float) -> None:
        raise KeyboardInterrupt

    code, out, _ = cli.run(backend, sleep=interrupt)  # pressed during the pause after the first post
    assert code == EXIT_INTERRUPTED and "Run the same command again" in out
    assert not [f for f in cli.files() if f.endswith(".part")]
    assert set(cli.log()) == {post_id(3)}  # what finished before the interruption is kept
    assert backend.sessions_opened == backend.sessions_closed
    assert cli.run(backend_for(3))[0] == EXIT_OK and len(cli.log()) == 3  # and the next run completes the job


# --------------------------------------------------------------------- bad input


def test_a_video_link_is_pointed_to_the_web_page(cli: Cli) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = main([f"https://www.tiktok.com/@scout2015/video/{post_id(1)}", "--out", str(cli.folder)],
                backend=backend_for(1), sleep=cli.sleeps.append, out=out, err=err)  # fmt: skip
    assert code == EXIT_FAILED and "video link" in err.getvalue() and not cli.folder.exists()


def test_a_link_that_is_not_tiktok_is_refused(cli: Cli) -> None:
    err = io.StringIO()
    code = main(["https://example.com/@someone", "--out", str(cli.folder)], backend=backend_for(1), out=io.StringIO(), err=err)
    assert code == EXIT_FAILED and "invalid_url" in err.getvalue()


def test_an_account_with_nothing_to_save_fails_with_the_reason(cli: Cli) -> None:
    code, _, err = cli.run(FakeBackend(error=VideoUnavailable("This account hasn't posted any public videos.")))
    assert code == EXIT_FAILED and "hasn't posted" in err and not cli.folder.exists()


def test_a_private_account_or_a_block_while_listing_is_a_clean_failure(cli: Cli) -> None:
    code, _, err = cli.run(FakeBackend(error=UpstreamBlocked()))
    assert code == EXIT_FAILED and "upstream_blocked" in err


def test_a_listing_cut_short_is_reported(cli: Cli) -> None:
    backend = FakeBackend(posts={post_id(1): make_post_info(1)}, listing=make_listing(1, warning="TikTok stopped the list early."))
    _, out, _ = cli.run(backend)
    assert "Warning: TikTok stopped the list early." in out and "run again later" in out


@pytest.mark.parametrize("args", [["--what", "video"], ["--what", ""], ["--limit", "-1"], ["--delay", "fast"]])
def test_bad_options_are_a_usage_error(cli: Cli, args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        cli.run(backend_for(1), *args)
    assert stop.value.code == 2 and "usage:" in capsys.readouterr().err


def test_the_output_path_may_not_be_a_file(tmp_path: Path) -> None:
    target = tmp_path / "taken"
    target.write_text("x")
    err = io.StringIO()
    code = main(["@scout2015", "--out", str(target)], backend=backend_for(1), out=io.StringIO(), err=err)
    assert code == EXIT_FAILED and "not a folder" in err.getvalue()


def test_a_bare_name_is_accepted_with_or_without_the_at_sign(cli: Cli) -> None:
    for name in ("scout2015", "@scout2015"):
        backend = backend_for(1)
        main([name, "--out", str(cli.folder), "--dry-run"], backend=backend, out=io.StringIO(), err=io.StringIO())
        assert backend.list_calls == [("scout2015", 1_000_000)]


# --------------------------------------------------------------- writing and the log


@pytest.mark.parametrize("path", ["../escape.mp4", "/etc/passwd", "a/../../b", "", "./x", "a//b", "..", "a/./b"])
def test_paths_that_could_leave_the_folder_are_refused(tmp_path: Path, path: str) -> None:
    with pytest.raises(ValueError, match="unsafe path"):
        write_file(tmp_path / "out", ArchiveFile.from_bytes(path, b"x"))
    assert not (tmp_path / "escape.mp4").exists()


@pytest.mark.parametrize("mtime", [10**12, 10**30, -10**12])
def test_a_date_the_file_system_rejects_does_not_fail_the_write(tmp_path: Path, mtime: int) -> None:
    target = write_file(tmp_path, ArchiveFile.from_bytes("clip.mp4", b"video", mtime=mtime))
    assert target.read_bytes() == b"video"


def test_a_write_that_fails_halfway_leaves_nothing_behind(tmp_path: Path) -> None:
    class Exploding(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            raise OSError("disk full")

    file = ArchiveFile("clip.mp4", 10, None, Exploding(b"0123456789"))
    with pytest.raises(OSError, match="disk full"):
        write_file(tmp_path, file)
    assert os.listdir(tmp_path) == []  # neither clip.mp4 nor clip.mp4.part


def test_files_are_written_atomically_into_subfolders(tmp_path: Path) -> None:
    target = write_file(tmp_path, ArchiveFile.from_bytes("a/b/c.txt", b"hello", 1_700_000_000))
    assert target == tmp_path / "a" / "b" / "c.txt" and target.read_bytes() == b"hello"
    assert target.stat().st_mtime == 1_700_000_000 and not list(tmp_path.rglob("*.part"))


def test_the_log_keeps_the_latest_record_per_post(tmp_path: Path) -> None:
    path = tmp_path / "posts.jsonl"
    lines = [{"id": "1", "status": "failed"}, {"id": "2", "status": "ok"}, {"id": "1", "status": "ok"}]
    path.write_text("\n".join(json.dumps(r) for r in lines) + "\nnot json\n[1, 2]\n{\"no_id\": 1}\n", encoding="utf-8")
    assert {k: v["status"] for k, v in load_log(path).items()} == {"1": "ok", "2": "ok"}
    assert load_log(tmp_path / "missing.jsonl") == {}


def test_plan_separates_settled_posts_from_the_ones_to_fetch() -> None:
    posts = make_listing(4).posts  # ids 4, 3, 2, 1
    latest = {post_id(4): {"id": post_id(4), "status": "ok"}, post_id(3): {"id": post_id(3), "status": "skipped"},
              post_id(2): {"id": post_id(2), "status": "failed"}, "7": {"id": "7", "status": "ok"}}  # fmt: skip
    todo, known = plan(posts, latest, retry_skipped=False)
    assert [p.id for p in todo] == [post_id(2), post_id(1)]
    assert {r["id"] for r in known} == {post_id(4), post_id(3), "7"}  # earlier runs stay in the manifest
    todo, known = plan(posts, latest, retry_skipped=True)
    assert [p.id for p in todo] == [post_id(3), post_id(2), post_id(1)]
    assert {r["id"] for r in known} == {post_id(4), "7"}
