"""Reseller web-portal auth: a Telegram one-time login link → a reseller session JWT.

Resellers have no password — their identity is their Telegram id (`Reseller.bot_chat_id`). The bot
(which shares SECRET_KEY with the backend) mints a short-lived SIGNED login token and gives the
reseller a link to the standalone site; the site exchanges it for a longer-lived session token
(`role="reseller"`). Every portal request is scoped to the reseller rows of that Telegram id — a
reseller can never see another reseller's or the owner's data. Owner tokens (`role="owner"`) are
rejected here, and these reseller tokens are rejected by the owner's `get_current_subject`.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import create_access_token, decode_token
from app.models import Reseller

PORTAL_ROLE = "reseller"
_LOGIN_TYP = "portal_login"
PORTAL_LOGIN_TTL_MIN = 15            # the one-time link is short-lived
# The browser session lasts 30 days and SLIDES: the portal client silently trades a still-valid
# token for a fresh one while it's in use (POST /api/portal/auth/refresh), so an active reseller
# never has to re-tap the bot. Revocation is immediate regardless of TTL — get_current_reseller
# re-checks the reseller rows on every request, so unbinding/deleting the reseller 401s at once.
PORTAL_SESSION_TTL_MIN = 30 * 24 * 60

# Reads the Bearer token; tokenUrl is only for docs (the real exchange is POST /api/portal/auth/exchange).
portal_oauth = OAuth2PasswordBearer(tokenUrl="api/portal/auth/exchange", auto_error=True)


def create_portal_login_token(chat_id: int) -> str:
    """Short-lived signed token embedded in the bot's login link. Carries a random `jti` so the
    exchange endpoint can make it strictly ONE-TIME (a second exchange of the same link is rejected)."""
    return create_access_token(
        str(chat_id),
        extra={"typ": _LOGIN_TYP, "jti": secrets.token_urlsafe(16)},
        expires_minutes=PORTAL_LOGIN_TTL_MIN,
    )


def create_portal_session_token(chat_id: int) -> str:
    """Longer-lived reseller session token issued after a valid login-token exchange."""
    return create_access_token(str(chat_id), extra={"role": PORTAL_ROLE},
                               expires_minutes=PORTAL_SESSION_TTL_MIN)


def verify_portal_login_token(token: str) -> tuple[int, str] | None:
    """Return (bot_chat_id, jti) from a valid login token, else None. The caller consumes the jti
    so the link works exactly once. Legacy tokens minted before jti existed return jti=""."""
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        return None
    if payload.get("typ") != _LOGIN_TYP:
        return None
    try:
        chat_id = int(str(payload.get("sub")))
    except (TypeError, ValueError):
        return None
    return chat_id, str(payload.get("jti") or "")


@dataclass
class ResellerContext:
    """The authenticated reseller (a Telegram id may own several reseller rows across panels)."""
    chat_id: int
    resellers: list[Reseller]

    @property
    def ids(self) -> list[int]:
        return [r.id for r in self.resellers]


async def get_current_reseller(
    token: str = Depends(portal_oauth),
    session: AsyncSession = Depends(get_session),
) -> ResellerContext:
    """Validate a reseller session token and load the caller's reseller rows. Raises 401 if the
    token is invalid/not a reseller token, or the Telegram id has no reseller rows (deregistered)."""
    err = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
    except jwt.PyJWTError as exc:
        raise err from exc
    if payload.get("role") != PORTAL_ROLE:
        raise err
    try:
        chat_id = int(str(payload.get("sub")))
    except (TypeError, ValueError) as exc:
        raise err from exc
    resellers = list((await session.execute(
        select(Reseller).where(Reseller.bot_chat_id == chat_id)
    )).scalars().all())
    if not resellers:
        raise err
    return ResellerContext(chat_id=chat_id, resellers=resellers)
