"""Save a whole profile from the command line: ``python -m app.bulk @name --out ./name``.

Unlike the browser ZIP this has no post limit and no browser connection to keep alive, so it can wait
out TikTok's rate limiting for as long as it takes. It is meant to be run again and again: posts that
are already saved are skipped, so an interrupted or blocked run simply carries on next time.

Files are written to the output folder as they arrive, and ``posts.jsonl`` there is the log that makes
resuming possible. ``posts.json``, ``posts.csv`` and ``REPORT.txt`` are rewritten at the end of each run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, TextIO

import httpx

from .archive import ArchiveFile, Pacer, PostRef, Progress, Wants, archive_posts
from .backend import Backend, YtDlpBackend
from .config import load_settings
from .console import lenient
from .errors import AppError, InvalidRequest, InvalidURL
from .media import ProfilePost, iso_date
from .urls import resolve_target

LOG_NAME = "posts.jsonl"
MIN_DELAY = 1.0  # a floor: pacing is what keeps TikTok from blocking the run
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_BLOCKED, EXIT_INTERRUPTED = 0, 1, 2, 3, 130
_HANDLE = re.compile(r"@?[A-Za-z0-9_.\-]{1,100}")


# ------------------------------------------------------------------- the log


def load_log(path: Path) -> dict[str, dict[str, Any]]:
    """The latest record for each post id. A half-written last line (a crash) is ignored."""
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return latest
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("id"), str):
            latest[record["id"]] = record
    return latest


def append_log(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
        log.flush()
        os.fsync(log.fileno())  # survive a crash or a power cut right after


def write_file(root: Path, file: ArchiveFile) -> Path:
    """Write one finished file under ``root``, atomically, refusing any path that could escape it."""
    # Judge the raw text: PurePosixPath would quietly tidy "./x" and "a//b", hiding that the name was odd.
    parts = file.path.split("/")
    if any(part in {"", ".", ".."} for part in parts) or "\\" in file.path or "\0" in file.path:
        raise ValueError(f"unsafe path in archive: {file.path!r}")
    target = root.joinpath(*parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"unsafe path in archive: {file.path!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    try:
        with partial.open("wb") as out:
            for chunk in file.chunks():
                out.write(chunk)
        os.replace(partial, target)  # a file either exists completely or not at all
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    if file.mtime:
        try:
            os.utime(target, (file.mtime, file.mtime))  # file times = when the post was published
        except (OSError, OverflowError, ValueError):
            pass  # a nicety: a file system that rejects the date must not fail the run
    return target


def plan(
    posts: Sequence[ProfilePost], latest: dict[str, dict[str, Any]], *, retry_skipped: bool
) -> tuple[list[ProfilePost], list[dict[str, Any]]]:
    """Which posts still need fetching, and the records of earlier runs to carry into the manifest.

    "ok" posts are done. "skipped" ones (private, removed, too large) are not retried unless asked,
    or every re-run would re-check them for nothing. "failed" ones (TikTok misbehaved) always are."""
    done = {"ok"} if retry_skipped else {"ok", "skipped"}
    todo = [p for p in posts if latest.get(p.id, {}).get("status") not in done]
    todo_ids = {p.id for p in todo}
    known = [r for pid, r in latest.items() if pid not in todo_ids and r.get("status") in {"ok", "skipped"}]
    return todo, known


# ----------------------------------------------------------------------- run


def _handle_from(target: str, proxy: str | None, timeout: int) -> str:
    text = target.strip()
    if _HANDLE.fullmatch(text):
        return text.lstrip("@")
    with httpx.Client(
        timeout=timeout, proxy=proxy, follow_redirects=False, headers={"User-Agent": "facebookexternalhit/1.1"}
    ) as http:
        found = resolve_target(text, http)
    if found.profile is None:
        raise InvalidURL("That is a video link. This tool saves whole profiles; use the web page for single posts.")
    return found.profile


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{round(seconds)} seconds"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    return f"{seconds / 3600:.1f} hours"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.bulk",
        description="Save every public post of a TikTok profile (videos, photo posts, optionally sounds and covers) "
        "with a manifest of captions, dates and stats. Safe to re-run: saved posts are skipped.",
    )
    parser.add_argument("profile", help="a profile link (https://www.tiktok.com/@name), a short link, or just @name")
    parser.add_argument("--out", type=Path, help="output folder (default: ./tiktok-<name>)")
    parser.add_argument("--limit", type=int, default=0, metavar="N", help="only the newest N posts (default: all)")
    parser.add_argument("--what", default="media", help="comma list of: media, audio, cover (default: media)")
    parser.add_argument("--video", choices=("compatible", "best"), default="compatible",
                        help="which video file when TikTok offers several: 'compatible' is the best H.264 one, which "
                        "plays on any device (up to 720p); 'best' is the highest resolution (up to 1080p), which is "
                        "sometimes H.265, and some players cannot open that (default: compatible)")  # fmt: skip
    parser.add_argument("--delay", type=float, default=3.0, metavar="SECONDS",
                        help=f"pause between posts, varied a little (default 3, minimum {MIN_DELAY:g})")  # fmt: skip
    parser.add_argument("--max-backoff", type=float, default=900.0, metavar="SECONDS",
                        help="longest wait when TikTok pushes back (default 900)")  # fmt: skip
    parser.add_argument("--retry-skipped", action="store_true",
                        help="also retry posts earlier runs found private or removed")  # fmt: skip
    parser.add_argument("--dry-run", action="store_true", help="show what would be saved, then stop")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    backend: Backend | None = None,
    sleep: Callable[[float], None] = time.sleep,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    out, err = lenient(out or sys.stdout), lenient(err or sys.stderr)

    def say(text: str = "") -> None:
        print(text, file=out, flush=True)

    def fail(text: str) -> None:
        print(text, file=err, flush=True)

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        wants = Wants.parse(args.what)
    except InvalidRequest as exc:
        parser.error(exc.message)
    if args.video == "best":
        wants = replace(wants, best=True)
    if args.limit < 0:
        parser.error("--limit must be zero or more")
    delay = max(MIN_DELAY, args.delay)
    if delay != args.delay:
        say(f"Using the minimum pause of {MIN_DELAY:g} second between posts: going faster is how connections get blocked.")

    settings = load_settings()
    backend = backend or YtDlpBackend(settings)
    try:
        handle = _handle_from(args.profile, settings.proxy_url, settings.network_timeout)
        say(f"Listing @{handle}'s posts…")
        listing = backend.list_profile(handle, args.limit or 1_000_000)
    except AppError as exc:
        fail(f"Failed: [{exc.code}] {exc.message}")
        return EXIT_FAILED
    if listing.warning:
        say(f"Warning: {listing.warning} Only the posts below are covered; run again later for the rest.")

    folder = args.out or Path(f"tiktok-{handle}")
    if folder.exists() and not folder.is_dir():
        fail(f"Failed: {folder} exists and is not a folder.")
        return EXIT_FAILED
    log_path = folder / LOG_NAME
    todo, known = plan(listing.posts, load_log(log_path), retry_skipped=args.retry_skipped)

    dated = [d for d in (iso_date(p.timestamp) for p in listing.posts) if d]
    span = f" ({min(dated)} to {max(dated)})" if dated else ""
    say(f"{len(listing.posts)} posts listed{span}; {len(listing.posts) - len(todo)} already saved or settled; {len(todo)} to fetch.")
    if todo:
        say(f"Roughly {_duration(len(todo) * (delay + 4))} at one post every {delay:g} seconds plus transfer time.")
    if args.dry_run or not todo:
        if not todo:
            say("Nothing to do.")
        return EXIT_OK

    folder.mkdir(parents=True, exist_ok=True)  # only now: a dry run, or nothing to do, leaves no trace
    progress = Progress(len(todo))
    counter = {"n": 0}

    def wait(seconds: float) -> bool:
        if seconds >= 20:
            say(f"  Waiting {_duration(seconds)} before trying again…")
        sleep(seconds)
        return False

    pacer = Pacer(wait, delay=delay, backoff_start=60.0, backoff_max=max(60.0, args.max_backoff), max_blocks=5)

    def record_done(record: dict[str, Any]) -> None:
        append_log(log_path, record)
        counter["n"] += 1
        status = record["status"]
        label = (record.get("posted_at") or "")[:10] or "----------"
        detail = f"{len(record.get('files', []))} file(s)" if status == "ok" else (record.get("reason") or status)
        caption = (record.get("caption") or "").replace("\n", " ")
        say(f"[{counter['n']:>4}/{len(todo)}] {label}  {record['id']}  {status}: {detail}" + (f'  "{caption[:50]}"' if caption else ""))

    files = archive_posts(
        backend, [PostRef(p.id, p.url) for p in todo], wants,
        pacer=pacer, progress=progress, max_bytes=settings.max_download_mb * 1024 * 1024,
        temp_dir=settings.temp_dir, known=known, on_record=record_done,
    )  # fmt: skip
    try:
        for file in files:
            try:
                write_file(folder, file)
            finally:
                file.close()
    except KeyboardInterrupt:
        say("\nInterrupted. Run the same command again to carry on where it stopped.")
        return EXIT_INTERRUPTED
    finally:
        files.close()

    snap = progress.snapshot()
    say(f"\nSaved {snap['saved']} of {snap['total']}" + (f", {snap['skipped']} unavailable" if snap["skipped"] else "")
        + (f", {snap['failed']} failed" if snap["failed"] else "") + f". Files and manifest are in {folder}")  # fmt: skip
    if snap["state"] == "aborted":
        fail(f"Stopped early: {snap['message']}")
        fail("Run the same command again later to carry on: posts that are already saved are skipped.")
        return EXIT_BLOCKED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
