from __future__ import annotations

import gc

import pytest

from app.backend import Download
from app.config import Settings, load_settings
from app.limits import Gate, SlidingWindowLimiter, TTLCache


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------------- limiter


def test_limiter_allows_up_to_the_limit_then_reports_how_long_to_wait() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(3, 60, clock=clock)
    assert [limiter.check("ip") for _ in range(3)] == [None, None, None]
    clock.now += 10
    assert limiter.check("ip") == pytest.approx(50)  # the oldest hit expires 60s after it was made


def test_limiter_window_slides() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(2, 60, clock=clock)
    limiter.check("ip")
    clock.now += 30
    limiter.check("ip")
    assert limiter.check("ip") is not None
    clock.now += 31  # first hit is now 61s old
    assert limiter.check("ip") is None


def test_rejected_hits_do_not_extend_the_block() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(1, 60, clock=clock)
    limiter.check("ip")
    for _ in range(50):
        clock.now += 1
        assert limiter.check("ip") is not None
    clock.now += 11  # 61s after the one accepted hit
    assert limiter.check("ip") is None


def test_limiter_keys_are_independent_and_zero_disables() -> None:
    limiter = SlidingWindowLimiter(1, 60, clock=Clock())
    assert limiter.check("a") is None
    assert limiter.check("b") is None
    assert limiter.check("a") is not None
    off = SlidingWindowLimiter(0)
    assert all(off.check("a") is None for _ in range(100))


def test_limiter_forgets_idle_keys_so_memory_stays_bounded() -> None:
    clock = Clock()
    limiter = SlidingWindowLimiter(5, 60, clock=clock, max_keys=10)
    for i in range(10):
        limiter.check(f"ip{i}")
    clock.now += 120
    limiter.check("fresh")
    limiter.check("fresh2")
    assert len(limiter._hits) <= 3


# ----------------------------------------------------------------------- cache


def test_cache_entries_expire() -> None:
    clock = Clock()
    cache: TTLCache[str, int] = TTLCache(10, 60, clock=clock)
    cache.set("a", 1)
    assert cache.get("a") == 1
    clock.now += 61
    assert cache.get("a") is None


def test_cache_evicts_least_recently_used() -> None:
    cache: TTLCache[str, int] = TTLCache(2, 60, clock=Clock())
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")  # "b" is now the oldest
    cache.set("c", 3)
    assert (cache.get("a"), cache.get("b"), cache.get("c")) == (1, None, 3)


def test_zero_ttl_disables_the_cache() -> None:
    cache: TTLCache[str, int] = TTLCache(2, 0)
    cache.set("a", 1)
    assert cache.get("a") is None


# ------------------------------------------------------------------------ gate


def test_gate_fails_fast_when_full_and_recovers_on_release() -> None:
    gate = Gate(1)
    assert gate.acquire()
    assert not gate.acquire()
    assert not gate.acquire(timeout=0.01)
    gate.release()
    assert gate.acquire()


# -------------------------------------------------------------------- Download


def test_download_cleanups_run_once_in_registration_order() -> None:
    calls: list[str] = []
    download = Download(filename="f", media_type="x/y", length=None, reader=lambda n: b"",
                        cleanups=[lambda: calls.append("first")])  # fmt: skip
    download.add_cleanup(lambda: calls.append("second"))
    download.close()
    download.close()
    assert calls == ["first", "second"]  # inner resources are released before the things that own them


def test_a_failing_cleanup_does_not_stop_the_others() -> None:
    calls: list[str] = []

    def boom() -> None:
        raise RuntimeError("nope")

    download = Download(filename="f", media_type="x/y", length=None, reader=lambda n: b"",
                        cleanups=[lambda: calls.append("ran"), boom])  # fmt: skip
    download.close()
    assert calls == ["ran"]


def test_a_download_that_is_never_read_still_cleans_up_when_garbage_collected() -> None:
    """Covers a client that disconnects before the first byte is sent."""
    calls: list[str] = []
    download = Download(filename="f", media_type="x/y", length=None, reader=lambda n: b"",
                        cleanups=[lambda: calls.append("released")])  # fmt: skip
    del download
    gc.collect()
    assert calls == ["released"]


# ----------------------------------------------------------------------- config


def test_defaults_are_conservative() -> None:
    s = Settings()
    assert s.host == "127.0.0.1" and s.access_log is False and s.allow_private_upstream is False


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "SNAPTIK_HOST": "0.0.0.0", "SNAPTIK_PORT": "9000", "SNAPTIK_RATE_LIMIT_LOOKUP": "5",
        "SNAPTIK_ACCESS_LOG": "yes", "SNAPTIK_PROXY_URL": "http://proxy:3128", "SNAPTIK_LOG_LEVEL": "debug",
        "SNAPTIK_MAX_DOWNLOAD_MB": "50",
    }.items():  # fmt: skip
        monkeypatch.setenv(key, value)
    s = load_settings()
    assert (s.host, s.port, s.rate_limit_lookup, s.access_log) == ("0.0.0.0", 9000, 5, True)
    assert (s.proxy_url, s.log_level, s.max_download_mb) == ("http://proxy:3128", "DEBUG", 50)


def test_plain_port_variable_is_used_as_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SNAPTIK_PORT", raising=False)
    monkeypatch.setenv("PORT", "8123")
    assert load_settings().port == 8123
    monkeypatch.setenv("SNAPTIK_PORT", "9001")
    assert load_settings().port == 9001  # the specific variable wins


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, "chrome"), ("", "chrome"), ("   ", "chrome"), ("firefox", "firefox"), ("chrome:macos", "chrome:macos"),
     ("off", ""), ("OFF", ""), ("none", ""), ("no", ""), ("false", ""), ("0", "")],
)  # fmt: skip
def test_browser_impersonation_for_listing_a_profile_is_on_by_default_and_can_be_turned_off(
    monkeypatch: pytest.MonkeyPatch, value: str | None, expected: str
) -> None:
    monkeypatch.delenv("SNAPTIK_IMPERSONATE", raising=False)
    if value is not None:
        monkeypatch.setenv("SNAPTIK_IMPERSONATE", value)
    assert load_settings().impersonate == expected


def test_blank_values_fall_back_to_defaults_and_bad_numbers_fail_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SNAPTIK_HOST", "   ")
    assert load_settings().host == "127.0.0.1"
    monkeypatch.setenv("SNAPTIK_RATE_LIMIT_LOOKUP", "lots")
    with pytest.raises(ValueError, match="SNAPTIK_RATE_LIMIT_LOOKUP"):
        load_settings()
