"""Helpers for tests that need a local stand-in "CDN" and real media files."""
from __future__ import annotations

import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from app.backend import Download

needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg/ffprobe not installed"
)


def read_all(download: Download) -> bytes:
    chunks = []
    while chunk := download.read(65536):
        chunks.append(chunk)
    return b"".join(chunks)


class Cdn:
    """A tiny HTTP server that serves fixed bodies and records every request it receives."""

    def __init__(self, routes: dict[str, tuple[int, str, bytes]]) -> None:
        self.routes = routes
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:  # noqa: N802
                path, _, query = self.path.partition("?")
                outer.requests.append(
                    {"path": path, "query": query, "headers": {k.lower(): v for k, v in self.headers.items()}}
                )
                status, content_type, body = outer.routes.get(path, (404, "text/plain", b"not found"))
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                if path.startswith("/nolength"):  # body ends when the connection closes
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                else:
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass  # the client gave up (size-limit tests)

            def log_message(self, *args: object) -> None:
                pass

        class QuietServer(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request: Any, client_address: Any) -> None:
                pass  # clients hanging up early is exactly what several tests do

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.02), daemon=True).start()

    def fetched(self) -> list[str]:
        return [r["path"] for r in self.requests]

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
