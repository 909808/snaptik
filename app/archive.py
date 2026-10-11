"""Saving many posts at once: file naming, the manifest, polite pacing, and the loop that both the
web ZIP download and the command-line archiver run.

``archive_posts`` is a generator of finished files. It fetches one post at a time, pausing between
posts, backing off when TikTok pushes back, and giving up cleanly when it keeps doing so. Whoever
consumes it (a ZIP stream, a folder on disk) only has to write each file out and close it.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import random
import re
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, BinaryIO

from .backend import Backend, Download
from .errors import (
    AppError,
    FormatUnavailable,
    InvalidRequest,
    LoginRequired,
    TooLarge,
    UpstreamBlocked,
    UpstreamError,
    VideoUnavailable,
)
from .media import KIND_AUDIO, KIND_COVER, KIND_VIDEO, VideoInfo, post_record, posted_date

log = logging.getLogger(__name__)

CHUNK_SIZE = 64 * 1024
SPOOL_IN_MEMORY = 8 * 1024 * 1024  # bigger files spill to a temp file instead of living in RAM

# Failures that are about the post, not about TikTok being unhappy with us.
_PERMANENT = (VideoUnavailable, LoginRequired, FormatUnavailable, TooLarge)


# ---------------------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class Wants:
    media: bool = True  # the video, or all the photos of a slideshow
    audio: bool = False
    cover: bool = False

    @classmethod
    def parse(cls, text: str) -> Wants:
        names = {part.strip() for part in text.split(",") if part.strip()}
        unknown = names - {"media", "audio", "cover"}
        if unknown or not names:
            raise InvalidRequest("Choose what to save: videos/photos, sounds, covers.")
        return cls(media="media" in names, audio="audio" in names, cover="cover" in names)


@dataclass(frozen=True, slots=True)
class PostRef:
    id: str
    url: str


# ---------------------------------------------------------------------------- naming


def slugify(text: str, limit: int = 40) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")[:limit].rstrip("-")


def post_stem(info: VideoInfo, ref: PostRef) -> str:
    """``2024-03-14_7123456789_pov-you-finally-figured-out``: sorts by date, stays unique by id."""
    parts = [posted_date(info) or "undated", ref.id]  # the id that was asked for (validated), not what TikTok reports
    if slug := slugify(info.caption or info.description or info.title):
        parts.append(slug)
    return "_".join(parts)


# ---------------------------------------------------------------------------- files


@dataclass(slots=True)
class ArchiveFile:
    """A finished file waiting to be written out. The consumer must ``close()`` it."""

    path: str  # relative, forward slashes, made only of safe characters
    size: int
    mtime: int | None
    data: BinaryIO  # positioned at the start

    @classmethod
    def from_bytes(cls, path: str, payload: bytes, mtime: int | None = None) -> ArchiveFile:
        return cls(path, len(payload), mtime, io.BytesIO(payload))

    def chunks(self, size: int = CHUNK_SIZE) -> Iterator[bytes]:
        while chunk := self.data.read(size):
            yield chunk

    def close(self) -> None:
        self.data.close()


def spool(download: Download, path: str, *, mtime: int | None, max_bytes: int, temp_dir: str | None) -> ArchiveFile:
    """Fetch a download completely before it goes into the archive, so a transfer that fails halfway
    can never leave a truncated file behind that looks complete."""
    buffer = tempfile.SpooledTemporaryFile(max_size=SPOOL_IN_MEMORY, dir=temp_dir)  # noqa: SIM115
    total = 0
    try:
        while chunk := download.read(CHUNK_SIZE):
            total += len(chunk)
            if total > max_bytes:
                raise TooLarge()
            buffer.write(chunk)
        if download.length is not None and total != download.length:
            raise UpstreamError("TikTok cut the transfer short.")
        buffer.seek(0)
        return ArchiveFile(path, total, mtime, buffer)  # type: ignore[arg-type]
    except BaseException:
        buffer.close()
        raise
    finally:
        download.close()


# ---------------------------------------------------------------------------- progress


class Progress:
    """What a running archive is doing, safe to read from another thread (the page polls it)."""

    def __init__(self, total: int, cancel: threading.Event | None = None) -> None:
        self.cancel = cancel or threading.Event()
        self.finished_at: float | None = None  # monotonic time the run ended, for tidying up old records
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {
            "state": "running",  # running | done | stopped | aborted
            "total": total,
            "done": 0,
            "saved": 0,
            "skipped": 0,
            "failed": 0,
            "current": None,
            "message": "",
        }

    def begin(self, post_id: str) -> None:
        with self._lock:
            self._data["current"] = post_id
            self._data["message"] = f"Saving post {self._data['done'] + 1} of {self._data['total']}"

    def message(self, text: str) -> None:
        with self._lock:
            self._data["message"] = text

    def completed(self, status: str) -> None:
        with self._lock:
            self._data["done"] += 1
            key = {"ok": "saved", "skipped": "skipped"}.get(status, "failed")
            self._data[key] += 1
            self._data["current"] = None

    def finish(self, state: str, message: str = "") -> None:
        with self._lock:
            if self._data["state"] == "running":
                self._data["state"] = state
                self._data["message"] = message
                self._data["current"] = None
                self.finished_at = time.monotonic()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)


# ---------------------------------------------------------------------------- pacing


@dataclass(frozen=True, slots=True)
class Decision:
    action: str  # "retry" | "skip" | "abort"
    wait: float = 0.0
    reason: str = ""
    permanent: bool = False  # the post itself is unavailable (as opposed to TikTok being unhappy with us)


class Pacer:
    """Keeps a bulk run polite: a pause between posts, exponential backoff when TikTok pushes back,
    and a clean stop when it keeps doing so. No evasion: it only ever slows down.

    ``wait(seconds)`` does the sleeping and returns True if the run was cancelled meanwhile; it is
    injected so the web path can wake up instantly and tests never really sleep.
    """

    def __init__(
        self,
        wait: Callable[[float], bool],
        *,
        delay: float = 3.0,
        jitter: float = 0.5,
        backoff_start: float = 30.0,
        backoff_max: float = 900.0,
        max_blocks: int = 4,
        max_retries: int = 2,
        max_failed_in_a_row: int = 5,
        rng: random.Random | None = None,
    ) -> None:
        self.wait = wait
        self.delay = delay
        self.jitter = jitter
        self.backoff_start = backoff_start
        self.backoff_max = backoff_max
        self.max_blocks = max_blocks
        self.max_retries = max_retries
        self.max_failed_in_a_row = max_failed_in_a_row
        self._rng = rng or random.Random()
        self._blocks = 0
        self._failed_in_a_row = 0

    def gap(self) -> float:
        """The pause between two posts: the base delay, randomly varied so it doesn't tick like a metronome."""
        return self.delay * self._rng.uniform(1 - self.jitter, 1 + self.jitter)

    def decide(self, exc: AppError, retries_used: int) -> Decision:
        if isinstance(exc, _PERMANENT):
            self.healthy()  # TikTok answered normally; the post is just gone
            return Decision("skip", reason=exc.message, permanent=True)
        if isinstance(exc, UpstreamBlocked):
            self._blocks += 1
            if self._blocks > self.max_blocks:
                return Decision(
                    "abort",
                    reason="TikTok keeps refusing requests from this connection, so the run was stopped. "
                    "Wait a while before trying again.",
                )
            pause = min(self.backoff_max, self.backoff_start * 2 ** (self._blocks - 1))
            return Decision("retry", wait=pause * self._rng.uniform(1.0, 1.25), reason=exc.message)
        if retries_used < self.max_retries:  # a hiccup (network, conversion): try again shortly
            return Decision("retry", wait=max(1.0, self.delay) * 2 * self._rng.uniform(1.0, 1.5), reason=exc.message)
        self._failed_in_a_row += 1
        if self._failed_in_a_row >= self.max_failed_in_a_row:
            return Decision(
                "abort",
                reason=f"{self._failed_in_a_row} posts in a row failed, so the run was stopped instead of hammering TikTok.",
            )
        return Decision("skip", reason=exc.message)

    def healthy(self) -> None:
        self._blocks = 0
        self._failed_in_a_row = 0


# ---------------------------------------------------------------------------- the loop


@dataclass(slots=True)
class _PostResult:
    record: dict[str, Any]
    abort: str | None = None
    cancelled: bool = False


def _extension(download: Download) -> str:
    return download.filename.rsplit(".", 1)[-1]


def _fetch_post(
    backend: Backend, ref: PostRef, wants: Wants, max_bytes: int, temp_dir: str | None
) -> tuple[list[ArchiveFile], dict[str, Any]]:
    """Look one post up once and fetch everything wanted from it. Extras (sound, cover, a single
    photo) may fail without losing the post; if the main media fails, the post fails."""
    files: list[ArchiveFile] = []
    warnings: list[str] = []
    try:
        with backend.open_post(ref.url) as post:
            info = post.info
            stem = post_stem(info, ref)
            slideshow = bool(info.images)
            extras = f"{stem}/" if slideshow else f"{stem}_"  # photos live in a folder, a video sits beside its extras

            def add(download: Download, path: str) -> None:
                files.append(spool(download, path, mtime=info.timestamp, max_bytes=max_bytes, temp_dir=temp_dir))

            if wants.media and slideshow:
                for index in range(len(info.images)):
                    try:
                        download = post.open_image(index)
                        add(download, f"{stem}/{index + 1:02d}.{_extension(download)}")
                    except AppError as exc:
                        warnings.append(f"photo {index + 1}: {exc.message}")
                if not files:
                    raise FormatUnavailable("None of this post's photos could be downloaded.")
            elif wants.media:
                download = post.open(KIND_VIDEO)
                add(download, f"{stem}.mp4")

            for wanted, kind, label in ((wants.audio, KIND_AUDIO, "sound"), (wants.cover, KIND_COVER, "cover")):
                if not wanted:
                    continue
                try:
                    download = post.open(kind)
                    add(download, f"{extras}{label}.{_extension(download)}")
                except AppError as exc:
                    warnings.append(f"{label}: {exc.message}")
            if not files:
                raise FormatUnavailable("Nothing could be saved for this post with the chosen options.")
            record = {**post_record(info), "id": ref.id, "status": "ok", "files": [f.path for f in files], "warnings": warnings}
    except BaseException:
        for file in files:
            file.close()
        raise
    return files, record


def _failure(ref: PostRef, status: str, reason: str) -> dict[str, Any]:
    return {"id": ref.id, "url": ref.url, "status": status, "reason": reason}


def _save_post(
    backend: Backend,
    ref: PostRef,
    wants: Wants,
    pacer: Pacer,
    progress: Progress,
    max_bytes: int,
    temp_dir: str | None,
) -> Generator[ArchiveFile, None, _PostResult]:
    retries = 0
    while True:
        try:
            files, record = _fetch_post(backend, ref, wants, max_bytes, temp_dir)
        except AppError as exc:
            decision = pacer.decide(exc, retries)
            log.info("post %s: %s -> %s", ref.id, exc.message, decision.action)
            if decision.action == "retry":
                retries += 1
                progress.message(f"TikTok pushed back. Waiting {round(decision.wait)}s before trying again.")
                if pacer.wait(decision.wait):
                    return _PostResult(_failure(ref, "skipped", "Stopped before this post was saved."), cancelled=True)
                progress.begin(ref.id)
                continue
            if decision.action == "abort":
                return _PostResult(_failure(ref, "failed", exc.message), abort=decision.reason)
            return _PostResult(_failure(ref, "skipped" if decision.permanent else "failed", decision.reason))
        pacer.healthy()
        try:
            yield from files
        finally:
            for file in files:  # whatever the consumer did not get to
                file.close()
        return _PostResult(record)


def archive_posts(
    backend: Backend,
    refs: Sequence[PostRef],
    wants: Wants,
    *,
    pacer: Pacer,
    progress: Progress,
    max_bytes: int,
    temp_dir: str | None = None,
    known: Sequence[Mapping[str, Any]] = (),
    on_record: Callable[[dict[str, Any]], None] | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[ArchiveFile]:
    """Yield every file of every post, one post at a time, then ``posts.json``, ``posts.csv`` and
    ``REPORT.txt``. ``known`` are records from earlier runs, merged into the manifest."""
    records: list[dict[str, Any]] = [dict(r) for r in known]
    state, note = "stopped", "The connection was closed."  # what is reported if the consumer leaves mid-run
    try:
        for index, ref in enumerate(refs):
            if progress.cancel.is_set():
                state, note = "stopped", "Stopped on request."
                break
            if deadline is not None and clock() > deadline:
                state, note = "stopped", "The time limit for one download was reached."
                break
            if index:
                progress.message(f"Pausing between posts so TikTok doesn't block this connection ({index} of {len(refs)} done)")
                if pacer.wait(pacer.gap()):
                    state, note = "stopped", "Stopped on request."
                    break
            progress.begin(ref.id)
            result = yield from _save_post(backend, ref, wants, pacer, progress, max_bytes, temp_dir)
            records.append(result.record)
            progress.completed(result.record["status"])
            if on_record:
                on_record(result.record)
            if result.abort:
                state, note = "aborted", result.abort
                break
            if result.cancelled:
                state, note = "stopped", "Stopped on request."
                break
        else:
            state, note = "done", ""
        for name, payload in manifest_files(records, state, note):
            yield ArchiveFile.from_bytes(name, payload)
    finally:
        progress.finish(state, note or "Done")


# ---------------------------------------------------------------------------- manifest

CSV_COLUMNS = [
    "id", "url", "status", "type", "posted_at", "caption", "hashtags", "author", "views", "likes",
    "comments", "shares", "duration", "sound", "photos", "files", "note",
]  # fmt: skip


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")


def _cell(value: Any) -> Any:
    """Spreadsheets run a cell that starts with = + - or @ as a formula, and captions are written by
    strangers: ``=HYPERLINK(...)`` as a caption must arrive as text. A leading apostrophe is the usual
    defence (posts.json keeps every caption exactly as written)."""
    return "'" + value if isinstance(value, str) and value.startswith(_FORMULA_START) else value


def _csv_row(record: Mapping[str, Any]) -> list[Any]:
    stats = record.get("stats") or {}
    sound = record.get("sound") or {}
    author = record.get("author") or {}
    notes = [record["reason"]] if record.get("reason") else list(record.get("warnings") or [])
    return [_cell(value) for value in (
        record.get("id"), record.get("url"), record.get("status"), record.get("type"), record.get("posted_at"),
        record.get("caption"), " ".join(record.get("hashtags") or []), author.get("handle"),
        stats.get("views"), stats.get("likes"), stats.get("comments"), stats.get("shares"), record.get("duration"),
        " – ".join(p for p in (sound.get("title"), ", ".join(sound.get("artists") or [])) if p),
        record.get("photo_count"), "; ".join(record.get("files") or []), "; ".join(notes),
    )]  # fmt: skip


def manifest_csv(records: Sequence[Mapping[str, Any]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for record in records:
        writer.writerow(_csv_row(record))
    return out.getvalue().encode("utf-8-sig")  # the BOM makes Excel read emoji and non-Latin captions correctly


def report_text(records: Sequence[Mapping[str, Any]], state: str, note: str, *, now: datetime | None = None) -> str:
    saved = [r for r in records if r.get("status") == "ok"]
    problems = [r for r in records if r.get("status") != "ok"]
    when = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"SnapTik archive report, {when}", "", f"Saved {len(saved)} of {len(records)} posts."]
    if note and state != "done":
        lines += ["", f"The run ended early: {note}"]
    if problems:
        lines += ["", "Not saved:"]
        lines += [f"  {r.get('id')}: {r.get('reason') or r.get('status')}" for r in problems]
    warned = [r for r in saved if r.get("warnings")]
    if warned:
        lines += ["", "Saved with problems:"]
        lines += [f"  {r.get('id')}: {'; '.join(r['warnings'])}" for r in warned]
    lines += ["", "posts.json and posts.csv list every post with its caption, date, stats and file names."]
    return "\n".join(lines) + "\n"


def _newest_first(records: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return sorted(records, key=lambda r: r.get("posted_at") or "", reverse=True)


def manifest_files(records: Sequence[Mapping[str, Any]], state: str, note: str) -> list[tuple[str, bytes]]:
    ordered = _newest_first(records)
    return [
        ("posts.json", json.dumps(ordered, ensure_ascii=False, indent=2).encode()),
        ("posts.csv", manifest_csv(ordered)),
        ("REPORT.txt", report_text(records, state, note).encode()),
    ]
