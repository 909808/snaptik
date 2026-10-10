"""FastAPI application: HTTP routes, error handling and security headers."""
from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .backend import CHUNK_SIZE, Backend, Download, YtDlpBackend, sweep_stale_tempdirs
from .config import Settings, load_settings
from .errors import AppError
from .service import Service

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Everything the page loads is same-origin, except thumbnails from TikTok's CDN.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data: https:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}

_NONCE = re.compile(r"^[A-Za-z0-9]{6,32}$")


class SecurityHeadersMiddleware:
    """Plain ASGI middleware (unlike BaseHTTPMiddleware it never interferes with streaming)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RevalidatingStaticFiles(StaticFiles):
    """Assets are not fingerprinted, so make browsers revalidate (cheap 304s thanks to ETag)."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


class LookupRequest(BaseModel):
    url: str = Field(min_length=1, max_length=8192)


def _error_response(status: int, code: str, message: str, retry_after: int | None = None) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status, headers=headers)


def _client_ip(request: Request) -> str:
    # Behind a reverse proxy, run uvicorn with --proxy-headers / FORWARDED_ALLOW_IPS so
    # this is the real client address rather than the proxy's.
    return request.client.host if request.client else "unknown"


async def _stream(download: Download) -> AsyncIterator[bytes]:
    try:
        while chunk := await run_in_threadpool(download.read, CHUNK_SIZE):
            yield chunk
    finally:
        download.close()


def create_app(settings: Settings | None = None, backend: Backend | None = None) -> FastAPI:
    settings = settings or load_settings()
    service = Service(settings, backend or YtDlpBackend(settings))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        sweep_stale_tempdirs(settings.temp_dir)
        yield
        service.close()

    app = FastAPI(title="SnapTik", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.service = service
    app.add_middleware(SecurityHeadersMiddleware)

    @app.exception_handler(AppError)
    async def handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return _error_response(exc.status_code, exc.code, exc.message, exc.retry_after)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_: Request, __: RequestValidationError) -> JSONResponse:
        return _error_response(400, "invalid_request", "Please paste a TikTok video link.")

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return _error_response(500, "internal_error", AppError.default_message)

    @app.post("/api/info")
    async def lookup(body: LookupRequest, request: Request) -> JSONResponse:
        service.check_rate("lookup", _client_ip(request))
        payload = await run_in_threadpool(service.lookup, body.url)
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.get("/api/download")
    async def download(
        request: Request,
        url: str = Query(..., max_length=8192),
        kind: str = Query("video", max_length=16),
        n: str | None = Query(None, max_length=32),
    ) -> Response:
        service.check_rate("download", _client_ip(request))
        item = await run_in_threadpool(service.open_download, url, kind)
        headers = {"Content-Disposition": f'attachment; filename="{item.filename}"', "Cache-Control": "no-store"}
        if item.length is not None:
            headers["Content-Length"] = str(item.length)
        response = StreamingResponse(
            _stream(item), media_type=item.media_type, headers=headers, background=BackgroundTask(item.close)
        )
        if n and _NONCE.match(n):
            # Lets the page learn that the browser has started saving the file (see app.js).
            response.set_cookie(f"dl_{n}", "1", max_age=30, path="/", samesite="lax")
        return response

    # HEAD is allowed too: uptime monitors and link checkers commonly use it.
    @app.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/robots.txt", include_in_schema=False)
    async def robots() -> FileResponse:
        return FileResponse(STATIC_DIR / "robots.txt", media_type="text/plain")

    app.mount("/static", RevalidatingStaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
