"""
First-run setup wizard (public, one-time).

Before the owner exists, the SPA shows a setup page instead of login. POST /api/setup
creates the owner, optionally sets the domain (→ Caddy auto-HTTPS), and marks
`setup_done=True`. After that the endpoint is locked (409) and the wizard never shows
again — only the normal captcha login works.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from pydantic import BaseModel

from app.core.db import get_session
from app.core.security import hash_password, validate_new_password
from app.models.app_user import AppUser
from app.models.setting import Setting
from app.services import settings_service

router = APIRouter(prefix="/api/setup", tags=["setup"])
_setup_lock = asyncio.Lock()


class SetupStatus(BaseModel):
    setup_done: bool
    domain: str = ""
    https_enabled: bool = False


class SetupRequest(BaseModel):
    username: str
    password: str
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
    )


@router.post("")
async def do_setup(body: SetupRequest, session: AsyncSession = Depends(get_session)) -> dict:
    # The process lock protects the current single-worker deployment and SQLite tests.
    # The row lock also serializes setup across PostgreSQL workers/processes.
    async with _setup_lock:
        await session.execute(
            select(Setting).where(Setting.key == "setup_done").with_for_update()
        )
        if await _is_done(session):
            raise HTTPException(409, "راه‌اندازی قبلاً انجام شده است.")
        username = (body.username or "").strip()
        if len(username) < 3:
            raise HTTPException(400, "نام کاربری باید حداقل ۳ کاراکتر باشد.")
        try:
            validate_new_password(body.password)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        session.add(
            AppUser(
                username=username,
                password_hash=hash_password(body.password),
                role="owner",
            )
        )
        await settings_service.set_value(session, "setup_done", True)

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
