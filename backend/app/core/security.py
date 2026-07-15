"""Password hashing (bcrypt) and JWT auth (PyJWT) for the owner web panel."""
from __future__ import annotations

import datetime as dt
import logging

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import loginsec
from app.core.config import settings
from app.core.db import get_session
from app.models.app_user import AppUser

ALGORITHM = "HS256"
OWNER_ROLE = "owner"
MIN_PASSWORD_CHARS = 8
MAX_PASSWORD_BYTES = 72
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")
log = logging.getLogger("security")


# ----------------------------- passwords -----------------------------
def validate_new_password(password: str) -> None:
    if not password or len(password) < MIN_PASSWORD_CHARS:
        raise ValueError(f"رمز عبور باید حداقل {MIN_PASSWORD_CHARS} کاراکتر باشد.")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"رمز عبور برای bcrypt باید حداکثر {MAX_PASSWORD_BYTES} بایت باشد."
        )


def hash_password(password: str) -> str:
    validate_new_password(password)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ------------------------------- JWT ---------------------------------
def create_access_token(
    subject: str, extra: dict | None = None, *, expires_minutes: int | None = None
) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    ttl = expires_minutes if expires_minutes is not None else settings.access_token_expire_minutes
    payload: dict = {
        "sub": subject,
        "iat": now,
        "exp": now + dt.timedelta(minutes=ttl),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])


def require_secure_transport(request: Request) -> None:
    """F2 (Strict): reject a credential/bearer request that arrives over plaintext HTTP from a
    non-loopback client. A no-op over real HTTPS (Caddy sets X-Forwarded-Proto=https) or from a
    loopback caller (SSH tunnel / in-container health). No settings/DB read — the decision is purely
    the forwarded proto + the direct peer, so it's safe on the hot path of every authenticated call."""
    proto = request.headers.get("x-forwarded-proto", "") or request.url.scheme
    ip = request.client.host if request.client else ""
    if not loginsec.secure_or_loopback_ok(proto, ip):
        raise HTTPException(426, "برای این درخواست، اتصال امن (HTTPS) لازم است.")


async def get_current_subject(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
    request: Request = None,  # type: ignore[assignment]  # FastAPI injects; None only for direct/internal calls
) -> str:
    """Returns the authenticated owner's username, or raises 401.

    Beyond signature/expiry, the token is bound to the account's live state: a deactivated
    account is rejected, and a password change (which bumps `token_epoch`) invalidates any
    token carrying an older `epoch` claim — so a stolen token dies the moment the owner
    changes their password.

    F2: every bearer request is transport-gated too — a JWT must not ride cleartext (`request` is
    injected by FastAPI on real HTTP; it's None only for direct programmatic/test calls)."""
    if request is not None:
        require_secure_transport(request)
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
    except jwt.PyJWTError as exc:
        raise credentials_error from exc
    subject = payload.get("sub")
    token_role = payload.get("role")
    token_epoch = payload.get("epoch")
    if (
        not isinstance(subject, str)
        or not subject
        or token_role != OWNER_ROLE
        or not isinstance(token_epoch, int)
        or isinstance(token_epoch, bool)
    ):
        raise credentials_error
    try:
        user = (
            await session.execute(select(AppUser).where(AppUser.username == subject))
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 — auth must fail closed on DB uncertainty
        log.exception("live account validation failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service temporarily unavailable",
        ) from exc
    if user is None:
        raise credentials_error
    if not user.is_active or user.role != OWNER_ROLE or token_role != user.role:
        raise credentials_error
    if token_epoch != int(user.token_epoch or 0):
        raise credentials_error
    return subject
