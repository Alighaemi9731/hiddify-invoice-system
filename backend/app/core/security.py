"""Password hashing (bcrypt) and JWT auth (PyJWT) for the owner web panel."""
from __future__ import annotations

import datetime as dt
import logging

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
def create_access_token(subject: str, extra: dict | None = None) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload: dict = {
        "sub": subject,
        "iat": now,
        "exp": now + dt.timedelta(minutes=settings.access_token_expire_minutes),
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])


async def get_current_subject(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
) -> str:
    """Returns the authenticated owner's username, or raises 401.

    Beyond signature/expiry, the token is bound to the account's live state: a deactivated
    account is rejected, and a password change (which bumps `token_epoch`) invalidates any
    token carrying an older `epoch` claim — so a stolen token dies the moment the owner
    changes their password."""
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
    except jwt.PyJWTError:
        raise credentials_error
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
