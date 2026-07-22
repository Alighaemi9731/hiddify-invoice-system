"""Request-duration logging — the observability gap found by the 2026-07-22 audit.

Before this, the backend had NO per-request latency signal at all: when the panel felt
slow there was no way to tell a slow endpoint from a blocked event loop from network
RTT. One structured line per API request now answers that. Raw ASGI (not
BaseHTTPMiddleware) so the wrapper itself adds no per-request task/stream overhead.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("http")

# Paths that are polled by infrastructure (container healthcheck every 10s) or the SPA
# (update polling) — logged only when slow, to keep the log signal:noise useful.
_QUIET_PATHS = {"/health", "/api/info", "/api/ops/update-status"}
_SLOW_MS = 1500.0


class TimingMiddleware:
    def __init__(self, app) -> None:  # noqa: ANN001 - ASGI app
        self.app = app

    async def __call__(self, scope, receive, send):  # noqa: ANN001 - ASGI signature
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status_code = 0

        async def send_wrapper(message):  # noqa: ANN001
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            dur_ms = (time.perf_counter() - start) * 1000
            path = scope.get("path", "")
            method = scope.get("method", "")
            if dur_ms >= _SLOW_MS:
                log.warning("SLOW %s %s -> %s %.0fms", method, path, status_code, dur_ms)
            elif path not in _QUIET_PATHS:
                log.info("%s %s -> %s %.0fms", method, path, status_code, dur_ms)
