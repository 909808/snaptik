"""Runtime configuration, read from ``SNAPTIK_*`` environment variables.

Every setting has a default, so ``python -m app`` works out of the box.
``PORT`` is honoured as a fallback for hosting platforms that set it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    access_log: bool = False  # off by default: access logs contain the pasted TikTok URLs

    # Abuse protection (per client IP, per minute; 0 disables a limit).
    rate_limit_lookup: int = 20
    rate_limit_download: int = 30
    max_concurrent_lookups: int = 6
    max_concurrent_downloads: int = 8
    max_download_mb: int = 200

    cache_ttl: int = 300  # seconds a lookup result is reused
    cache_size: int = 512

    network_timeout: int = 20  # seconds, for every outbound request
    proxy_url: str | None = None  # route TikTok traffic through this proxy
    ffmpeg_path: str = "ffmpeg"
    ffmpeg_timeout: int = 90
    temp_dir: str | None = None

    # Tests only: lets the streaming code talk to a server on localhost.
    allow_private_upstream: bool = False


def _env(name: str) -> str | None:
    value = os.environ.get(f"SNAPTIK_{name}")
    if value is None or not value.strip():
        return None
    return value.strip()


def _int(name: str, default: int, *, fallback_env: str | None = None) -> int:
    raw = _env(name)
    if raw is None and fallback_env:
        raw = os.environ.get(fallback_env, "").strip() or None
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        raise ValueError(f"SNAPTIK_{name} must be a whole number, got {raw!r}") from None


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    d = Settings()
    return Settings(
        host=_env("HOST") or d.host,
        port=_int("PORT", d.port, fallback_env="PORT"),
        log_level=(_env("LOG_LEVEL") or d.log_level).upper(),
        access_log=_bool("ACCESS_LOG", d.access_log),
        rate_limit_lookup=_int("RATE_LIMIT_LOOKUP", d.rate_limit_lookup),
        rate_limit_download=_int("RATE_LIMIT_DOWNLOAD", d.rate_limit_download),
        max_concurrent_lookups=max(1, _int("MAX_CONCURRENT_LOOKUPS", d.max_concurrent_lookups)),
        max_concurrent_downloads=max(1, _int("MAX_CONCURRENT_DOWNLOADS", d.max_concurrent_downloads)),
        max_download_mb=max(1, _int("MAX_DOWNLOAD_MB", d.max_download_mb)),
        cache_ttl=_int("CACHE_TTL", d.cache_ttl),
        cache_size=max(1, _int("CACHE_SIZE", d.cache_size)),
        network_timeout=max(1, _int("NETWORK_TIMEOUT", d.network_timeout)),
        proxy_url=_env("PROXY_URL"),
        ffmpeg_path=_env("FFMPEG_PATH") or d.ffmpeg_path,
        ffmpeg_timeout=max(1, _int("FFMPEG_TIMEOUT", d.ffmpeg_timeout)),
        temp_dir=_env("TEMP_DIR"),
    )
