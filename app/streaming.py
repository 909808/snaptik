"""Bridges a long-running blocking generator to an async HTTP response, with cancellation."""
from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

_DONE = object()


class BlockingStream:
    """Runs ``produce(cancel)`` in its own thread and exposes the chunks it yields as an async iterator.

    * Backpressure: the thread waits when the client is slow (a small queue sits in between).
    * Cancellation: ``cancel`` is set when the consumer goes away (client disconnect) *or* when
      someone sets it (a "stop" button). The producer is expected to wind down; if the consumer
      has gone it is also stopped from emitting more.
    * The generator is closed in the thread that ran it, so its ``finally`` blocks always execute.
    """

    def __init__(
        self,
        produce: Callable[[threading.Event], Iterator[bytes]],
        *,
        cancel: threading.Event | None = None,
        buffer: int = 4,
    ) -> None:
        self._produce = produce
        self.cancel = cancel or threading.Event()
        self._buffer = buffer

    async def __aiter__(self) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue(self._buffer)
        gone = threading.Event()  # the consumer has left: nothing more will be read

        def put(item: Any) -> bool:
            if gone.is_set():
                return False
            try:
                asyncio.run_coroutine_threadsafe(queue.put(item), loop).result()
            except Exception:  # the loop is shutting down
                return False
            return not gone.is_set()

        def run() -> None:
            chunks = None
            try:
                chunks = iter(self._produce(self.cancel))
                for chunk in chunks:
                    if not put(chunk):
                        return
                put(_DONE)
            except BaseException as exc:  # noqa: BLE001 - handed to the consumer
                put(exc)
            finally:
                close = getattr(chunks, "close", None)
                if close:
                    close()

        threading.Thread(target=run, name="blocking-stream", daemon=True).start()
        try:
            while True:
                item = await queue.get()
                if item is _DONE:
                    return
                if isinstance(item, BaseException):
                    raise item
                yield item
        finally:
            gone.set()
            self.cancel.set()
            while not queue.empty():  # unblock a producer that is waiting for room
                queue.get_nowait()
