"""Live check against TikTok: ``python -m app.smoke <tiktok-url> [--save DIR]``.

The automated tests cannot reach tiktok.com, so this is how to verify, from *your* network,
that extraction and each download type work after installing or upgrading yt-dlp.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx

from .backend import Backend, YtDlpBackend
from .config import load_settings
from .console import lenient
from .errors import AppError
from .media import build_options
from .urls import resolve_target


def _size(num_bytes: int) -> str:
    return f"{num_bytes / 1_048_576:.2f} MiB" if num_bytes >= 1_048_576 else f"{num_bytes / 1024:.1f} KiB"


def main(argv: Sequence[str] | None = None, *, backend: Backend | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.smoke", description=__doc__.split("\n")[0])
    parser.add_argument("url", help="a TikTok video link (full or short)")
    parser.add_argument("--save", metavar="DIR", type=Path, help="also download every offered file into DIR")
    args = parser.parse_args(argv)
    lenient(sys.stdout)  # author names and captions are arbitrary text
    lenient(sys.stderr)

    settings = load_settings()
    backend = backend or YtDlpBackend(settings)
    try:
        with httpx.Client(
            timeout=settings.network_timeout, proxy=settings.proxy_url, headers={"User-Agent": "facebookexternalhit/1.1"}
        ) as http:
            target = resolve_target(args.url, http)
        if target.profile is not None:
            print(f"That is a profile link; check profiles with: python -m app.bulk @{target.profile} --dry-run", file=sys.stderr)
            return 2
        print(f"post      {target.canonical_url}")

        info = backend.fetch_info(target.canonical_url)
        print(f"author    {info.author_name} (@{info.author_handle})")
        print(f"caption   {info.description[:80]!r}")
        print(f"duration  {info.duration}s, {len(info.formats)} formats, {len(info.covers)} covers")

        options = build_options(info, ffmpeg_available=backend.ffmpeg_available)
        if not options:
            print("FAIL: TikTok returned nothing downloadable for this post", file=sys.stderr)
            return 1
        print("offers")
        for option in options:
            print(f"  {option.kind:9s} {option.title:16s} {option.detail}  [{option.tag or '-'}]")

        if args.save:
            args.save.mkdir(parents=True, exist_ok=True)
            print(f"downloads -> {args.save}")
            for option in options:
                download = backend.open_download(target.canonical_url, option.kind)
                path = args.save / download.filename
                try:
                    with path.open("wb") as out:
                        while chunk := download.read():
                            out.write(chunk)
                finally:
                    download.close()
                if download.length is not None and path.stat().st_size != download.length:
                    print(f"FAIL: {option.kind}: got {path.stat().st_size} bytes, expected {download.length}", file=sys.stderr)
                    return 1
                print(f"  {option.kind:9s} {_size(path.stat().st_size):>10s}  {path.name}")
    except AppError as exc:
        print(f"FAIL: [{exc.code}] {exc.message}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
