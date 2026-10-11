"""A minimal streaming ZIP writer: builds an archive piece by piece, with no seeking and no temp file.

Entries are DEFLATE-compressed (level 1): the standard format for streamed zips, which every
unzip tool reads. Media barely compresses, but the CPU cost is small next to the network time.
"""
from __future__ import annotations

import io
import time
import zipfile
from collections.abc import Iterable, Iterator

# The ZIP format can only represent dates from 1980 to the end of 2107.
_EARLIEST, _LATEST = 315_532_800, 4_354_819_199


class _Sink(io.RawIOBase):
    """Collects what zipfile writes so it can be handed on to the HTTP response as it appears."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, data: bytes) -> int:  # type: ignore[override]
        self._chunks.append(bytes(data))
        return len(data)

    def drain(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data


class ZipStream:
    """Each ``add_*`` and ``finish`` is a generator of the archive bytes produced so far."""

    def __init__(self, *, compresslevel: int = 1) -> None:
        self._sink = _Sink()
        self._zip = zipfile.ZipFile(
            self._sink, "w", zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=compresslevel
        )
        self._closed = False

    @staticmethod
    def _entry(path: str, mtime: float | None) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(path, time.gmtime(min(max(mtime or time.time(), _EARLIEST), _LATEST))[:6])
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        return info

    def _drain(self) -> Iterator[bytes]:
        if data := self._sink.drain():
            yield data

    def add_bytes(self, path: str, data: bytes, mtime: float | None = None) -> Iterator[bytes]:
        self._zip.writestr(self._entry(path, mtime), data)
        yield from self._drain()

    def add_stream(self, path: str, chunks: Iterable[bytes], mtime: float | None = None) -> Iterator[bytes]:
        with self._zip.open(self._entry(path, mtime), "w") as entry:
            for chunk in chunks:
                entry.write(chunk)
                yield from self._drain()
        yield from self._drain()

    def finish(self) -> Iterator[bytes]:
        """Write the central directory. The archive is only valid once this has been consumed."""
        self.close()
        yield from self._drain()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._zip.close()
        except ValueError:
            pass  # an entry was still open because the caller stopped midway; the archive is abandoned


class IterReader:
    """Adapts an iterator of byte chunks to the ``read(size)`` shape that ``Download`` expects."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks

    def read(self, size: int = -1) -> bytes:
        return next(self._chunks, b"")
