"""FastAPI application entrypoint."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app import __version__
from app.api import (
    auth,
    invoices,
    meta,
    operations,
    panels,
    passkey,
    payments,
    reports,
    resellers,
)
from app.api import (
    settings as settings_api,
)
from app.api import (
    setup as setup_api,
)
from app.core.config import settings
from app.services.invoice_state import InvoiceStateError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # First-boot/startup: apply versioned DB migrations, then seed owner + settings.
    from app.services.bootstrap import run_bootstrap

    await run_bootstrap()

    if settings.run_scheduler:
        from app.scheduler import scheduler

        await scheduler.start()

        # Self-restart if a restore performed by the bot process changed the DB / SECRET_KEY,
        # so the backend never keeps a stale key or a pooled handle to the pre-restore DB.
        from app.services import restart_signal

        restart_signal.start_watcher()

    yield

    if settings.run_scheduler:
        from app.scheduler import scheduler

        await scheduler.shutdown()


# In production, don't expose the interactive docs / OpenAPI schema (it enumerates every
# endpoint and shape to an unauthenticated visitor). They stay on in dev for convenience.
_is_prod = settings.app_env == "production"
app = FastAPI(
    title="Hiddify Reseller Invoicing System",
    version=__version__,
    lifespan=lifespan,
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
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

# Illegal invoice state transitions (B03) surface as a clean 400 with a Persian message.
@app.exception_handler(InvoiceStateError)
async def _invoice_state_error_handler(_request: Request, exc: InvoiceStateError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


app.include_router(meta.router)
app.include_router(setup_api.router)
app.include_router(auth.router)
app.include_router(passkey.router)
app.include_router(panels.router)
app.include_router(resellers.router)
app.include_router(invoices.router)
app.include_router(payments.router)
app.include_router(reports.router)
app.include_router(operations.router)
app.include_router(settings_api.router)
# The bot runs as a separate process; the scheduler runs in this process.
