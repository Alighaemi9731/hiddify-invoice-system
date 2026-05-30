from __future__ import annotations

from pydantic import BaseModel


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    username: str
    role: str
    totp_enabled: bool = False


class LoginRequest(BaseModel):
    username: str
    password: str
    captcha_id: str
    captcha_answer: str
    totp_code: str | None = None  # required only when 2FA is enabled


class CaptchaOut(BaseModel):
    captcha_id: str
    image: str  # data: URI PNG


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class AccountUpdate(BaseModel):
    current_password: str
    new_username: str | None = None
    new_password: str | None = None


class TotpSetupOut(BaseModel):
    secret: str
    otpauth_uri: str
    qr: str  # data: URI PNG


class TotpEnable(BaseModel):
    code: str  # 6-digit code to confirm before enabling


class TotpDisable(BaseModel):
    current_password: str
