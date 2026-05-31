"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app import __version__
from app.api import (
    auth,
    invoices,
    meta,
    operations,
    panels,
    payments,
    reports,
    resellers,
    setup as setup_api,
    settings as settings_api,
)
from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # First-boot: create tables, seed owner + settings.
    from app.services.bootstrap import run_bootstrap

    await run_bootstrap()

    if settings.run_scheduler:
        from app.scheduler import scheduler

        await scheduler.start()

    yield

    if settings.run_scheduler:
        from app.scheduler import scheduler

        await scheduler.shutdown()


app = FastAPI(
    title="Hiddify Reseller Invoicing System",
    version=__version__,
    lifespan=lifespan,
)

# Security headers on every response.
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        resp.headers.setdefault("X-XSS-Protection", "0")
        if settings.app_env == "production":
            resp.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return resp


app.add_middleware(SecurityHeadersMiddleware)

# CORS. In production the SPA is same-origin (served via Caddy), so cross-origin is not
# needed and we keep it CLOSED (only the configured domain, if any). In local dev we
# allow any origin for the Vite server.
if settings.app_env != "production":
    _cors_origins = ["*"]
elif getattr(settings, "server_domain", ""):
    _cors_origins = [f"https://{settings.server_domain}"]
else:
    _cors_origins = []  # same-origin only (behind Caddy); do NOT fall open to "*"
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(meta.router)
app.include_router(setup_api.router)
app.include_router(auth.router)
app.include_router(panels.router)
app.include_router(resellers.router)
app.include_router(invoices.router)
app.include_router(payments.router)
app.include_router(reports.router)
app.include_router(operations.router)
app.include_router(settings_api.router)
# The bot runs as a separate process; the scheduler runs in this process.
