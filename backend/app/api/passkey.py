"""Passkey (WebAuthn) — register a Face ID / Touch ID / security-key credential and use it
for passwordless, captcha-less login. Password + captcha remain as a fallback path.

rp_id / origin come from the configured `server_domain` (passkeys are domain-bound), so a
domain must be set first. Challenges live in an in-memory TTL store (single worker)."""
from __future__ import annotations

import base64
import json
import logging

import webauthn
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from app.api.auth import _client_ip
from app.core import loginsec, webauthn_store
from app.core.db import get_session
from app.core.security import OWNER_ROLE, create_access_token, get_current_subject
from app.models.app_user import AppUser
from app.models.webauthn_credential import WebauthnCredential
from app.schemas.auth import Token
from app.services import settings_service

log = logging.getLogger("passkey")
router = APIRouter(prefix="/api/auth/passkey", tags=["passkey"])

# Passkey login is usernameless; rate-limit it per-IP under a synthetic username so a
# challenge/assertion flood is throttled the same way password login is.
_PK_USER = "__passkey__"


def _check_locked(request: Request) -> str:
    ip = _client_ip(request)
    locked = loginsec.is_locked(_PK_USER, ip)
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"تعداد تلاش‌ها بیش از حد مجاز است. {locked // 60 + 1} دقیقه بعد دوباره تلاش کنید.",
        )
    return ip


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64url_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _token_for(user: AppUser) -> str:
    return create_access_token(user.username, {"role": user.role, "epoch": int(user.token_epoch or 0)})


async def _rp(session: AsyncSession) -> tuple[str, str]:
    domain = (await settings_service.get(session, "server_domain", "") or "").strip()
    if not domain:
        raise HTTPException(400, "ابتدا یک دامنه برای سامانه تنظیم کنید؛ ورود با Face ID به دامنه نیاز دارد.")
    return domain, f"https://{domain}"


async def _user(session: AsyncSession, username: str) -> AppUser:
    u = (await session.execute(select(AppUser).where(AppUser.username == username))).scalar_one_or_none()
    if not u:
        raise HTTPException(404, "حساب یافت نشد.")
    return u


# ----------------------------- registration (authenticated) -----------------------------
@router.post("/register/begin")
async def register_begin(
    session: AsyncSession = Depends(get_session), username: str = Depends(get_current_subject)
) -> dict:
    user = await _user(session, username)
    rp_id, _origin = await _rp(session)
    existing = (
        await session.execute(select(WebauthnCredential).where(WebauthnCredential.user_id == user.id))
    ).scalars().all()
    opts = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name="سامانه فاکتور",
        user_id=str(user.id).encode(),
        user_name=user.username,
        user_display_name=user.username,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,             # discoverable → usernameless login
            user_verification=UserVerificationRequirement.REQUIRED,   # force biometric/PIN
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=_b64url_dec(c.credential_id)) for c in existing
        ],
    )
    handle = webauthn_store.put(opts.challenge, username)
    return {"handle": handle, "options": json.loads(webauthn.options_to_json(opts))}


class RegisterComplete(BaseModel):
    handle: str
    credential: dict
    name: str | None = None


@router.post("/register/complete")
async def register_complete(
    body: RegisterComplete,
    session: AsyncSession = Depends(get_session),
    username: str = Depends(get_current_subject),
) -> dict:
    user = await _user(session, username)
    rp_id, origin = await _rp(session)
    taken = webauthn_store.take(body.handle)
    if not taken:
        raise HTTPException(400, "نشست منقضی شد؛ دوباره تلاش کنید.")
    challenge, _ = taken
    try:
        ver = webauthn.verify_registration_response(
            credential=body.credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            require_user_verification=True,
        )
    except Exception:  # noqa: BLE001 — details to the log only, never to the client
        log.warning("passkey registration verification failed", exc_info=True)
        raise HTTPException(
            400, "ثبت کلید عبور ناموفق بود. دوباره تلاش کنید."
        ) from None
    session.add(WebauthnCredential(
        user_id=user.id,
        credential_id=_b64url(ver.credential_id),
        public_key=base64.b64encode(ver.credential_public_key).decode(),
        sign_count=ver.sign_count,
        name=(body.name or "Face ID")[:64],
    ))
    await session.commit()
    return {"ok": True}


# ------------------------------- login (unauthenticated) -------------------------------
@router.post("/login/begin")
async def login_begin(request: Request, session: AsyncSession = Depends(get_session)) -> dict:
    _check_locked(request)
    rp_id, _origin = await _rp(session)
    opts = webauthn.generate_authentication_options(
        rp_id=rp_id, user_verification=UserVerificationRequirement.REQUIRED,
    )
    handle = webauthn_store.put(opts.challenge)
    return {"handle": handle, "options": json.loads(webauthn.options_to_json(opts))}


class LoginComplete(BaseModel):
    handle: str
    credential: dict


@router.post("/login/complete", response_model=Token)
async def login_complete(
    body: LoginComplete, request: Request, session: AsyncSession = Depends(get_session)
) -> Token:
    ip = _check_locked(request)
    rp_id, origin = await _rp(session)
    taken = webauthn_store.take(body.handle)
    if not taken:
        raise HTTPException(400, "نشست منقضی شد؛ دوباره تلاش کنید.")
    challenge, _ = taken
    raw_id = body.credential.get("id") or body.credential.get("rawId")
    cred = (
        await session.execute(select(WebauthnCredential).where(WebauthnCredential.credential_id == raw_id))
    ).scalar_one_or_none()
    if not cred:
        loginsec.record_failure(_PK_USER, ip)
        raise HTTPException(401, "این کلید عبور شناخته نشد.")
    try:
        ver = webauthn.verify_authentication_response(
            credential=body.credential,
            expected_challenge=challenge,
            expected_rp_id=rp_id,
            expected_origin=origin,
            credential_public_key=base64.b64decode(cred.public_key),
            credential_current_sign_count=cred.sign_count,
            require_user_verification=True,
        )
    except Exception:  # noqa: BLE001 — details to the log only
        loginsec.record_failure(_PK_USER, ip)
        log.warning("passkey login verification failed", exc_info=True)
        raise HTTPException(
            401, "ورود با کلید عبور ناموفق بود. دوباره تلاش کنید."
        ) from None
    user = await session.get(AppUser, cred.user_id)
    if not user or not user.is_active or user.role != OWNER_ROLE:
        raise HTTPException(401, "حساب غیرفعال است.")
    cred.sign_count = ver.new_sign_count
    await session.commit()
    loginsec.reset(_PK_USER, ip)
    return Token(access_token=_token_for(user))


# --------------------------------- manage (authenticated) ---------------------------------
@router.get("/list")
async def list_passkeys(
    session: AsyncSession = Depends(get_session), username: str = Depends(get_current_subject)
) -> list[dict]:
    user = await _user(session, username)
    rows = (
        await session.execute(select(WebauthnCredential).where(WebauthnCredential.user_id == user.id))
    ).scalars().all()
    return [{"id": c.id, "name": c.name, "created_at": c.created_at} for c in rows]


@router.delete("/{cred_id}")
async def delete_passkey(
    cred_id: int, session: AsyncSession = Depends(get_session), username: str = Depends(get_current_subject)
) -> dict:
    user = await _user(session, username)
    c = await session.get(WebauthnCredential, cred_id)
    # 404 whether it's missing OR not the caller's — never reveal another user's credential ids.
    if not c or c.user_id != user.id:
        raise HTTPException(404, "کلید عبور یافت نشد.")
    await session.delete(c)
    await session.commit()
    return {"ok": True}
