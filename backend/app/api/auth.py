"""Owner authentication: login (JWT) with captcha + rate-limit + optional 2FA,
profile, password/account change, and TOTP (Google Authenticator) management."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto, loginsec
from app.core.db import get_session
from app.core.security import (
    create_access_token,
    get_current_subject,
    hash_password,
    verify_password,
)
from app.models.app_user import AppUser
from app.schemas.auth import (
    AccountUpdate,
    CaptchaOut,
    LoginRequest,
    Token,
    TotpDisable,
    TotpEnable,
    TotpSetupOut,
    UserOut,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _client_ip(request: Request) -> str:
    # Our Caddy reverse proxy APPENDS the real peer IP to X-Forwarded-For, so the LAST
    # entry is the trustworthy one. Taking the first (leftmost) entry — which the client
    # fully controls — would let an attacker send a fresh fake IP per request and dodge the
    # per-IP lockout entirely, so we use the last hop instead.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        parts = [p.strip() for p in fwd.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "?"


async def _get_user(session: AsyncSession, username: str) -> AppUser | None:
    res = await session.execute(select(AppUser).where(AppUser.username == username))
    return res.scalar_one_or_none()


MIN_PASSWORD_LEN = 8


def _validate_password(pw: str) -> None:
    if not pw or len(pw) < MIN_PASSWORD_LEN:
        raise HTTPException(status_code=400, detail=f"رمز عبور باید حداقل {MIN_PASSWORD_LEN} کاراکتر باشد.")


def _token_for(user: AppUser) -> str:
    return create_access_token(user.username, {"role": user.role, "epoch": int(user.token_epoch or 0)})


@router.get("/captcha", response_model=CaptchaOut)
async def get_captcha() -> CaptchaOut:
    cid, img = loginsec.new_captcha()
    return CaptchaOut(captcha_id=cid, image=img)


@router.post("/login", response_model=Token)
async def login(body: LoginRequest, request: Request,
                session: AsyncSession = Depends(get_session)) -> Token:
    ip = _client_ip(request)

    # 1) lockout check
    locked = loginsec.is_locked(body.username, ip)
    if locked:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"تعداد تلاش‌ها بیش از حد مجاز است. {locked // 60 + 1} دقیقه بعد دوباره تلاش کنید.",
        )

    # 2) captcha (always required) — single-use
    if not loginsec.verify_captcha(body.captcha_id, body.captcha_answer):
        loginsec.record_failure(body.username, ip)
        raise HTTPException(status_code=400, detail="کد امنیتی نادرست است.")

    # 3) credentials
    user = await _get_user(session, body.username)
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        remaining = loginsec.record_failure(body.username, ip)
        detail = "نام کاربری یا رمز عبور نادرست است."
        if remaining and remaining <= 2:
            detail += f" ({remaining} تلاش باقی مانده)"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)

    # 4) 2FA (if enabled)
    if user.totp_enabled:
        secret = crypto.decrypt(user.totp_secret_enc)
        if not body.totp_code:
            # signal the client to ask for the code (without burning an attempt)
            raise HTTPException(status_code=401, detail="کد تأیید دو مرحله‌ای لازم است.",
                                headers={"X-2FA-Required": "1"})
        if not loginsec.verify_totp(secret, body.totp_code):
            loginsec.record_failure(body.username, ip)
            raise HTTPException(status_code=401, detail="کد تأیید دو مرحله‌ای نادرست است.")

    loginsec.reset(body.username, ip)
    return Token(access_token=_token_for(user))


@router.get("/me", response_model=UserOut)
async def me(subject: str = Depends(get_current_subject),
             session: AsyncSession = Depends(get_session)) -> UserOut:
    user = await _get_user(session, subject)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserOut(username=user.username, role=user.role, totp_enabled=user.totp_enabled)


@router.post("/account", response_model=Token)
async def update_account(body: AccountUpdate, subject: str = Depends(get_current_subject),
                         session: AsyncSession = Depends(get_session)) -> Token:
    """Change the owner's username and/or password. Returns a fresh token."""
    user = await _get_user(session, subject)
    if not user or not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="رمز عبور فعلی نادرست است")
    if body.new_username and body.new_username != user.username:
        if await _get_user(session, body.new_username):
            raise HTTPException(status_code=409, detail="این نام کاربری قبلاً استفاده شده است")
        user.username = body.new_username
        user.token_epoch = int(user.token_epoch or 0) + 1  # invalidate tokens with the old name
    if body.new_password:
        _validate_password(body.new_password)
        user.password_hash = hash_password(body.new_password)
        user.token_epoch = int(user.token_epoch or 0) + 1  # invalidate older tokens
    await session.commit()
    return Token(access_token=_token_for(user))


# ----------------------------- 2FA management -----------------------------
@router.post("/2fa/setup", response_model=TotpSetupOut)
async def totp_setup(subject: str = Depends(get_current_subject),
                     session: AsyncSession = Depends(get_session)) -> TotpSetupOut:
    """Generate a new TOTP secret + QR. Not enabled until confirmed via /2fa/enable."""
    user = await _get_user(session, subject)
    if not user:
        raise HTTPException(404, "User not found")
    secret = loginsec.new_totp_secret()
    user.totp_secret_enc = crypto.encrypt(secret)  # stored, enabled only after confirm
    await session.commit()
    uri = loginsec.totp_uri(secret, user.username)
    return TotpSetupOut(secret=secret, otpauth_uri=uri, qr=loginsec.totp_qr_data_uri(uri))


@router.post("/2fa/enable")
async def totp_enable(body: TotpEnable, subject: str = Depends(get_current_subject),
                      session: AsyncSession = Depends(get_session)) -> dict:
    user = await _get_user(session, subject)
    if not user or not user.totp_secret_enc:
        raise HTTPException(400, "ابتدا راه‌اندازی ۲ مرحله‌ای را شروع کنید.")
    secret = crypto.decrypt(user.totp_secret_enc)
    if not loginsec.verify_totp(secret, body.code):
        raise HTTPException(400, "کد واردشده نادرست است.")
    user.totp_enabled = True
    await session.commit()
    return {"status": "ok", "totp_enabled": True}


@router.post("/2fa/disable")
async def totp_disable(body: TotpDisable, subject: str = Depends(get_current_subject),
                       session: AsyncSession = Depends(get_session)) -> dict:
    user = await _get_user(session, subject)
    if not user or not verify_password(body.current_password, user.password_hash):
        raise HTTPException(400, "رمز عبور نادرست است.")
    user.totp_enabled = False
    user.totp_secret_enc = None
    await session.commit()
    return {"status": "ok", "totp_enabled": False}
