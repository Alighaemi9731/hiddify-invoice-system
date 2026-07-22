"""
First-run setup wizard (public, one-time).

Before the owner exists, the SPA shows a setup page instead of login. POST /api/setup
creates the owner, optionally sets the domain (→ Caddy auto-HTTPS), and marks
`setup_done=True`. After that the endpoint is locked (409) and the wizard never shows
again — only the normal captcha login works.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import loginsec
from app.core.db import get_session
from app.core.security import (
    hash_password_async,
    validate_new_password,
    verify_password_async,
)
from app.models.app_user import AppUser
from app.models.setting import Setting
from app.services import settings_service

router = APIRouter(prefix="/api/setup", tags=["setup"])
_setup_lock = asyncio.Lock()

_TOKEN_HASH_KEY = "setup_bootstrap_token_hash"


class SetupStatus(BaseModel):
    setup_done: bool
    domain: str = ""
    https_enabled: bool = False
    token_required: bool = False   # F1: the wizard must collect the installer's bootstrap token


class SetupRequest(BaseModel):
    username: str
    password: str
    token: str | None = None       # F1: one-time bootstrap token (required iff a hash is stored)
    domain: str | None = None
    acme_email: str | None = None


async def _is_done(session: AsyncSession) -> bool:
    if await settings_service.get(session, "setup_done", False):
        return True
    # Defensive: if an owner already exists, treat setup as done.
    return bool((await session.execute(select(func.count(AppUser.id)))).scalar_one())


@router.get("/status", response_model=SetupStatus)
async def status(session: AsyncSession = Depends(get_session)) -> SetupStatus:
    done = await _is_done(session)
    return SetupStatus(
        setup_done=done,
        domain=str(await settings_service.get(session, "server_domain", "") or ""),
        https_enabled=bool(await settings_service.get(session, "https_enabled", False)),
        token_required=bool(await settings_service.get(session, _TOKEN_HASH_KEY, "")),
    )


@router.post("")
async def do_setup(
    body: SetupRequest, request: Request, session: AsyncSession = Depends(get_session)
) -> dict:
    from app.api.auth import require_secure

    await require_secure(request)  # F2 (Strict): no owner password over plaintext from a non-loopback client
    # The process lock protects the current single-worker deployment and SQLite tests.
    # The row lock also serializes setup across PostgreSQL workers/processes.
    async with _setup_lock:
        await session.execute(
            select(Setting).where(Setting.key == "setup_done").with_for_update()
        )
        if await _is_done(session):
            raise HTTPException(409, "راه‌اندازی قبلاً انجام شده است.")
        # F1: read the bootstrap token but DO NOT consume it yet. Token clear, owner creation, and the
        # `setup_done` flag all commit TOGETHER at the end — so a validation failure below leaves the
        # token intact and setup re-runnable (closes the consume-then-fail takeover: previously the
        # token was cleared+committed before validation, so a bad username still burned the token and
        # a later no-token request could finish setup).
        token_hash = str(await settings_service.get(session, _TOKEN_HASH_KEY, "") or "")
        # F1 (fail-closed): a legacy install with NO minted token may only be set up from loopback
        # (SSH tunnel) — never an anonymous public request. Fresh installs always mint a token, so this
        # only affects a pre-token box, which must be reached via a domain (HTTPS) or an SSH tunnel.
        ip = request.client.host if request.client else ""
        if not token_hash and not loginsec.is_loopback(ip):
            raise HTTPException(
                403,
                "توکنِ راه‌اندازی تنظیم نشده است؛ نصب‌کننده را دوباره اجرا کنید یا از طریق تونل SSH وارد شوید.",
            )
        # Validate EVERY input before any persistent change (token still untouched here).
        username = (body.username or "").strip()
        if len(username) < 3:
            raise HTTPException(400, "نام کاربری باید حداقل ۳ کاراکتر باشد.")
        try:
            validate_new_password(body.password)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if token_hash and (
            not body.token or not await verify_password_async(body.token, token_hash)
        ):
            raise HTTPException(403, "توکنِ راه‌اندازی نادرست است.")

        # All inputs valid — create the owner, mark setup done, and consume the token atomically.
        session.add(
            AppUser(
                username=username,
                password_hash=await hash_password_async(body.password),
                role="owner",
            )
        )
        await settings_service.set_value(session, "setup_done", True, commit=False)
        if token_hash:
            await settings_service.set_value(session, _TOKEN_HASH_KEY, "", commit=False)  # one-time: consume it
        await session.commit()

    result: dict = {"setup_done": True, "domain_applied": False}
    if body.domain:
        from app.services import domain_setup

        dr = await domain_setup.set_domain(session, body.domain, body.acme_email)
        result["domain_applied"] = dr.get("ok", False)
        result["domain"] = dr.get("domain")
        result["url"] = dr.get("url")
        result["message"] = dr.get("message")
        result["detail"] = dr.get("detail")
    return result
