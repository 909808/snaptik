# SnapTik

A small, self-hosted web app for saving TikTok videos. Paste a link and get the video **without the watermark**, the **sound as MP3**, or the **cover image**. It is the TikTok counterpart of the many YouTube-downloader sites, built on [yt-dlp](https://github.com/yt-dlp/yt-dlp), FastAPI and a framework-free front end.

![SnapTik showing a lookup result (placeholder demo data)](docs/preview.png)

*Screenshot rendered with placeholder demo data, not a real TikTok post.*

## Features

- **Any link shape**: full links, short links (`vm.tiktok.com`, `vt.tiktok.com`, `tiktok.com/t/…`), embed links, or a whole pasted share text ("Check out … https://vm.tiktok.com/…").
- **Video**: MP4 without watermark at the best available quality, a lighter rendition when one exists, and the original watermarked file when TikTok offers it.
- **Audio**: the post's own sound track as an ID3-tagged MP3 (M4A if ffmpeg isn't installed). **Cover**: the post's cover image.
- **Photo slideshows**: sound and cover only (that is what yt-dlp can extract).
- **Real downloads**: files stream from TikTok to the browser as attachments. Nothing is buffered in memory or kept on the server.
- **Front end**: light/dark theme, mobile friendly, no framework, no build step, no third-party requests.
- **Hardening**: strict TikTok-only URL allow-list (no SSRF), strict CSP, per-client rate limits, concurrency and file-size caps.

## Quick start

Needs Python 3.10+. [ffmpeg](https://ffmpeg.org/) is optional but enables MP3 conversion.

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m app                      # http://127.0.0.1:8000
```

**Check it works from your network first.** TikTok blocks many datacenter IPs, and its site changes often, so before relying on it run:

```bash
python -m app.smoke "https://www.tiktok.com/@someone/video/1234567890123456789" --save /tmp/snaptik-out
```

It looks the post up, lists what would be offered, and (with `--save`) downloads every offered file.

### Docker

```bash
docker compose up --build -d       # http://127.0.0.1:8000
```

The image includes ffmpeg and runs as a non-root user with a read-only filesystem. Rebuild periodically (`docker compose build --pull --no-cache`) to pick up newer yt-dlp releases.

## Configuration

Environment variables, all optional:

| Variable | Default | Meaning |
|---|---|---|
| `SNAPTIK_HOST` / `SNAPTIK_PORT` (or `PORT`) | `127.0.0.1` / `8000` | Where to listen. |
| `SNAPTIK_RATE_LIMIT_LOOKUP` | `20` | Lookups per minute per client IP (`0` = unlimited). |
| `SNAPTIK_RATE_LIMIT_DOWNLOAD` | `30` | Downloads per minute per client IP. |
| `SNAPTIK_MAX_CONCURRENT_LOOKUPS` / `…_DOWNLOADS` | `6` / `8` | Simultaneous TikTok lookups / transfers; extra requests get a retryable 503. |
| `SNAPTIK_MAX_DOWNLOAD_MB` | `200` | Largest file the server will relay. |
| `SNAPTIK_CACHE_TTL` / `SNAPTIK_CACHE_SIZE` | `300` / `512` | How long (seconds) and how many lookups are remembered. |
| `SNAPTIK_NETWORK_TIMEOUT` | `20` | Seconds for every outbound request. |
| `SNAPTIK_PROXY_URL` | none | Send all TikTok traffic through this proxy (see Troubleshooting). |
| `SNAPTIK_FFMPEG_PATH` / `SNAPTIK_FFMPEG_TIMEOUT` | `ffmpeg` / `90` | ffmpeg binary and conversion time limit (seconds). |
| `SNAPTIK_TEMP_DIR` | system temp | Scratch space for MP3 conversion. |
| `SNAPTIK_LOG_LEVEL` | `INFO` | Python log level. |
| `SNAPTIK_ACCESS_LOG` | off | Enable uvicorn access logs (they contain the pasted TikTok URLs). |

## API

Both endpoints are what the page itself uses.

**`POST /api/info`** with `{"url": "<link or pasted text>"}` returns the post and what can be downloaded:

```json
{
  "id": "6718335390845095173",
  "url": "https://www.tiktok.com/@scout2015/video/6718335390845095173",
  "title": "…", "description": "…", "duration": 15,
  "author": {"name": "Scout", "handle": "scout2015"},
  "thumbnail": "https://…", "stats": {"views": 1234, "likes": 56, "comments": 8, "shares": 7},
  "note": null,
  "downloads": [
    {"kind": "video", "title": "Video · 1080p", "detail": "MP4 · H.264 · 6.6 MB", "tag": "No watermark",
     "ext": "mp4", "primary": true, "href": "/api/download?url=…&kind=video"}
  ]
}
```

**`GET /api/download?url=<link>&kind=<kind>`** streams the file as an attachment. `kind` is one of `video`, `video_sd`, `video_wm`, `audio`, `cover`; use the `href` values from `/api/info`, since not every kind exists for every post.

Errors are JSON, `{"error": {"code": "…", "message": "…"}}`:

| Status | `code` | When |
|---|---|---|
| 400 | `invalid_request`, `invalid_url` | Malformed request, or not a TikTok video link. |
| 403 | `login_required` | Private or age-restricted post. |
| 404 | `video_unavailable`, `format_unavailable` | Removed/private post, or that download doesn't exist for it. |
| 413 | `too_large` | File exceeds `SNAPTIK_MAX_DOWNLOAD_MB`. |
| 429 | `rate_limited` | Over the per-IP limit (`Retry-After` is set). |
| 502 | `upstream_error`, `upstream_blocked`, `conversion_failed` | TikTok didn't cooperate, or ffmpeg failed. |
| 503 | `busy` | Concurrency limit reached (`Retry-After` is set). |

## How it works

```
app/
  urls.py      validates pasted text, follows short links hop by hop (the trust boundary)
  media.py     turns yt-dlp's output into download options; pure logic, easy to test
  backend.py   yt-dlp lookup + streaming + MP3 conversion behind a small Backend protocol
  service.py   rate limits, concurrency gates, lookup cache
  main.py      FastAPI routes, error mapping, security headers
  smoke.py     `python -m app.smoke`, the live check described above
  static/      the page: index.html, style.css, app.js (no inline scripts or styles)
```

- Only a canonical `https://www.tiktok.com/@user/video/<id>` URL, rebuilt from validated parts, ever reaches yt-dlp, and only its TikTok extractor is used. Media URLs come from TikTok's own response, never from the client.
- A download re-runs the lookup in the same yt-dlp session that then fetches the file, so the CDN gets the cookies and headers it expects, and a dead CDN host falls back to the next one.
- The browser downloads through a hidden iframe: the file goes straight to disk. The server sets a short-lived cookie when the transfer starts, which is how the button knows to stop showing "Preparing…". Errors arrive as JSON and are shown as a toast.

## Deploying

Put a TLS-terminating reverse proxy in front. For nginx:

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_buffering off;        # stream downloads instead of spooling them to disk
    proxy_read_timeout 120s;
}
```

Run uvicorn with the proxy trusted so rate limits see real client IPs: the default trusts `127.0.0.1`; set `FORWARDED_ALLOW_IPS` to your proxy's address otherwise (use `*` only if the app is reachable solely through the proxy). Rate limits and caches live in memory, so each worker process keeps its own.

## Troubleshooting

- **`upstream_blocked` or `upstream_error` on every link.** TikTok often refuses datacenter IP ranges. Run it from a home connection, or set `SNAPTIK_PROXY_URL` to a (residential) proxy. Run `python -m app.smoke <url>` with `SNAPTIK_LOG_LEVEL=DEBUG` to see yt-dlp's own message.
- **It worked yesterday.** TikTok changed something. Upgrade yt-dlp (`pip install -U yt-dlp`, or rebuild the image) and retry.
- **The audio button says "M4A".** ffmpeg wasn't found; install it or set `SNAPTIK_FFMPEG_PATH`.
- **A photo slideshow only offers sound and cover.** yt-dlp can't extract the individual photos.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The suite needs no network access. It covers URL validation (including hostile inputs), format selection, rate limits, the HTTP API, and, with real yt-dlp and ffmpeg, streaming, CDN fallback, size caps, MP3 conversion and cleanup against a local server. `tests/test_ytdlp_tiktok_parser.py` runs yt-dlp's real TikTok parser on a TikTok-shaped fixture and fails loudly if a yt-dlp upgrade changes the format naming this app relies on.

What it **cannot** tell you is whether TikTok's live site still matches yt-dlp's extractor. Run `python -m app.smoke` against a real post after installing or upgrading.

## Legal

SnapTik fetches publicly available posts the way a browser would. You are responsible for how you use it: only save content you made or have permission to keep, use downloads for personal offline viewing, and respect creators' rights. TikTok's Terms of Service restrict automated access, and running a public instance can draw takedown requests. This project is not affiliated with, endorsed by, or sponsored by TikTok or ByteDance; TikTok is a trademark of its owner.
