"""The streaming ZIP writer and the cancellable thread-to-async bridge."""
from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import threading
import time
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.backend import Download
from app.streaming import BlockingStream
from app.zipstream import IterReader, ZipStream

# ---------------------------------------------------------------------------- zip


def build(entries: dict[str, bytes | list[bytes]], mtime: int | None = 1_700_000_000) -> bytes:
    archive = ZipStream()
    out = bytearray()
    for name, content in entries.items():
        chunks = content if isinstance(content, list) else None
        parts = archive.add_stream(name, chunks, mtime) if chunks is not None else archive.add_bytes(name, content, mtime)  # type: ignore[arg-type]
        for piece in parts:
            out += piece
    for piece in archive.finish():
        out += piece
    return bytes(out)


def test_a_streamed_archive_is_a_valid_zip_with_every_entry() -> None:
    data = build({
        "a.txt": b"hello",
        "videos/b.mp4": [b"x" * 70_000, b"y" * 70_000, b"z"],  # streamed in pieces
        "empty": b"",
        "deep/er/c.json": b'{"k": 1}',
    })  # fmt: skip
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == ["a.txt", "videos/b.mp4", "empty", "deep/er/c.json"]
        assert archive.read("a.txt") == b"hello"
        assert archive.read("videos/b.mp4") == b"x" * 70_000 + b"y" * 70_000 + b"z"
        assert archive.read("empty") == b"" and archive.read("deep/er/c.json") == b'{"k": 1}'


@pytest.mark.skipif(shutil.which("unzip") is None, reason="unzip is not installed")
def test_an_independent_unzip_reads_the_archive_too(tmp_path: Path) -> None:
    """Python's own reader is lenient; check with the tool people will actually use."""
    payload = bytes(range(256)) * 2000
    archive = tmp_path / "a.zip"
    archive.write_bytes(build({"one.bin": [payload[:1000], payload[1000:]], "dir/two.txt": b"two"}))
    test = subprocess.run(["unzip", "-t", str(archive)], capture_output=True, text=True, check=False)
    assert test.returncode == 0 and "No errors detected" in test.stdout
    subprocess.run(["unzip", "-q", str(archive), "-d", str(tmp_path / "out")], check=True)
    assert (tmp_path / "out" / "one.bin").read_bytes() == payload
    assert (tmp_path / "out" / "dir" / "two.txt").read_bytes() == b"two"


def test_entries_get_the_post_time_and_old_dates_are_clamped() -> None:
    data = build({"new": b"x"}, mtime=1_700_000_000)
    assert zipfile.ZipFile(io.BytesIO(data)).getinfo("new").date_time == (2023, 11, 14, 22, 13, 20)
    ancient = build({"old": b"x"}, mtime=5)  # before 1980, which zip cannot store
    assert zipfile.ZipFile(io.BytesIO(ancient)).getinfo("old").date_time[0] == 1980
    far_future = build({"late": b"x"}, mtime=10**12)  # after 2107, which it cannot store either
    assert zipfile.ZipFile(io.BytesIO(far_future)).getinfo("late").date_time[:5] == (2107, 12, 31, 23, 59)


def test_bytes_are_emitted_while_an_entry_is_still_being_fed() -> None:
    """The whole point of streaming: nothing waits for the end of the entry."""
    archive = ZipStream()
    emitted = 0
    emitted_before_last_chunk = 0

    def chunks() -> Iterator[bytes]:
        nonlocal emitted_before_last_chunk
        for index in range(6):
            if index == 5:
                emitted_before_last_chunk = emitted
            yield os.urandom(200_000)  # incompressible, so output appears as the data goes in

    for piece in archive.add_stream("big", chunks()):
        emitted += len(piece)
    assert emitted_before_last_chunk > 300_000


def test_abandoning_an_archive_midway_does_not_raise() -> None:
    archive = ZipStream()
    stream = archive.add_stream("half", iter([b"abc", b"def"]))
    next(stream, None)
    archive.close()  # an entry is still open: must be swallowed, not blow up cleanup code
    archive.close()


def test_iter_reader_feeds_a_download() -> None:
    reader = IterReader(iter([b"ab", b"cd"]))
    download = Download(filename="f", media_type="x/y", length=None, reader=reader.read)
    assert [download.read(10), download.read(10), download.read(10), download.read(10)] == [b"ab", b"cd", b"", b""]


# --------------------------------------------------------------------- the bridge


def collect(stream: BlockingStream) -> list[bytes]:
    async def go() -> list[bytes]:
        return [chunk async for chunk in stream]

    return asyncio.run(go())


def wait_for(condition, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_chunks_arrive_in_order() -> None:
    stream = BlockingStream(lambda cancel: iter([b"a", b"b", b"c"]))
    assert collect(stream) == [b"a", b"b", b"c"]


def test_the_producer_runs_in_its_own_thread_and_is_closed_there() -> None:
    seen: dict[str, str] = {}

    def produce(cancel: threading.Event) -> Iterator[bytes]:
        seen["during"] = threading.current_thread().name
        try:
            yield b"x"
        finally:
            seen["closed_in"] = threading.current_thread().name

    assert collect(BlockingStream(produce)) == [b"x"]
    assert seen == {"during": "blocking-stream", "closed_in": "blocking-stream"}


def test_a_failure_in_the_producer_reaches_the_consumer() -> None:
    def produce(cancel: threading.Event) -> Iterator[bytes]:
        yield b"ok"
        raise RuntimeError("boom")

    async def go() -> None:
        got = []
        with pytest.raises(RuntimeError, match="boom"):
            async for chunk in BlockingStream(produce):
                got.append(chunk)
        assert got == [b"ok"]

    asyncio.run(go())


def test_a_slow_consumer_holds_the_producer_back() -> None:
    produced = 0

    def produce(cancel: threading.Event) -> Iterator[bytes]:
        nonlocal produced
        for _ in range(1000):
            produced += 1
            yield b"x" * 10

    async def go() -> int:
        stream = BlockingStream(produce, buffer=3)
        it = stream.__aiter__()
        for _ in range(5):
            await it.__anext__()
        await asyncio.sleep(0.3)  # give the producer every chance to run ahead
        ahead = produced
        await it.aclose()
        return ahead

    assert asyncio.run(go()) <= 5 + 3 + 3  # consumed + queue + the item(s) in flight


def test_a_consumer_that_leaves_stops_the_producer_and_runs_its_cleanup() -> None:
    cleaned = threading.Event()
    cancel = threading.Event()

    def produce(c: threading.Event) -> Iterator[bytes]:
        try:
            while True:
                yield b"x" * 100
        finally:
            cleaned.set()

    async def go() -> None:
        stream = BlockingStream(produce, cancel=cancel, buffer=2)
        it = stream.__aiter__()
        await it.__anext__()
        await it.aclose()  # the client disconnected

    asyncio.run(go())
    assert wait_for(cleaned.is_set), "the producer was not stopped"
    assert cancel.is_set()  # and it was told to wind down


def test_a_stop_request_lets_the_producer_finish_gracefully_while_the_consumer_keeps_reading() -> None:
    cancel = threading.Event()

    def produce(c: threading.Event) -> Iterator[bytes]:
        index = 0
        while not c.is_set():
            index += 1
            yield f"chunk{index}".encode()
            if index == 3:
                cancel.set()  # the "stop" button
        yield b"trailer"  # the manifest still gets delivered

    chunks = collect(BlockingStream(produce, cancel=cancel))
    assert chunks == [b"chunk1", b"chunk2", b"chunk3", b"trailer"]


def test_a_producer_waiting_for_room_is_released_when_the_consumer_leaves() -> None:
    finished = threading.Event()

    def produce(c: threading.Event) -> Iterator[bytes]:
        try:
            for _ in range(10_000):
                yield b"x"
        finally:
            finished.set()

    async def go() -> None:
        it = BlockingStream(produce, buffer=1).__aiter__()
        await it.__anext__()
        await asyncio.sleep(0.1)  # the producer is now blocked on a full queue
        await it.aclose()

    asyncio.run(go())
    assert wait_for(finished.is_set)
