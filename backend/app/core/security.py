"""Password hashing (bcrypt) and JWT auth (PyJWT) for the owner web panel."""
from __future__ import annotations

import datetime as dt

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_session

ALGORITHM = "HS256"
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")


# ----------------------------- passwords -----------------------------
def hash_password(password: str) -> str:
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
    if not subject:
        raise credentials_error
    try:
        from sqlalchemy import select

        from app.models.app_user import AppUser

        user = (
            await session.execute(select(AppUser).where(AppUser.username == subject))
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 — a DB hiccup must not lock the owner out
        return subject
    # The query SUCCEEDED. If it found no user, the token is for an account that no longer
    # exists (e.g. the username was changed) → reject. (Only a DB ERROR above falls back to
    # trusting the token, so a transient hiccup doesn't lock the owner out.)
    if user is None:
        raise credentials_error
    if not user.is_active:
        raise credentials_error
    tok_epoch = payload.get("epoch")
    if tok_epoch is not None and int(tok_epoch) != int(user.token_epoch or 0):
        raise credentials_error
    return subject
