"""Small in-memory building blocks for abuse protection and caching.

State lives in the process, which is the right trade-off for a single small
instance. If you run several workers, each enforces its own limits.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class SlidingWindowLimiter:
    """Allow ``limit`` hits per ``window`` seconds for each key; ``limit <= 0`` disables it."""

    def __init__(
        self,
        limit: int,
        window: float = 60.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> float | None:
        """Record a hit. Returns ``None`` if allowed, else the seconds until a slot frees up."""
        if self.limit <= 0:
            return None
        now = self._clock()
        cutoff = now - self.window
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                return max(0.0, hits[0] + self.window - now)  # rejected hits are not recorded
            hits.append(now)
            if len(self._hits) > self._max_keys:
                for stale in [k for k, h in self._hits.items() if not h or h[-1] <= cutoff]:
                    del self._hits[stale]
            return None


class TTLCache(Generic[K, V]):
    """A small thread-safe LRU cache whose entries expire after ``ttl`` seconds."""

    def __init__(self, maxsize: int, ttl: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.maxsize = maxsize
        self.ttl = ttl
        self._clock = clock
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: K) -> V | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, value = item
            if expires <= self._clock():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: K, value: V) -> None:
        if self.ttl <= 0:
            return
        with self._lock:
            self._data[key] = (self._clock() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)


class Gate:
    """Bounded concurrency that fails fast instead of queueing forever."""

    def __init__(self, size: int) -> None:
        self._semaphore = threading.BoundedSemaphore(size)

    def acquire(self, timeout: float = 0) -> bool:
        if timeout <= 0:
            return self._semaphore.acquire(blocking=False)
        return self._semaphore.acquire(timeout=timeout)

    def release(self) -> None:
        self._semaphore.release()
