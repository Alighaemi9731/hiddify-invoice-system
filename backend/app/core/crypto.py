"""
Symmetric encryption for secret values stored in the DB (panel admin API keys,
bot token, wallet xpub). The Fernet key is derived from SECRET_KEY, so rotating
SECRET_KEY invalidates stored ciphertexts (re-enter secrets after rotation).
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_PREFIX = "enc::"


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.secret_key.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt(plaintext: str | None) -> str | None:
    """Encrypt a secret. Returns a value prefixed with `enc::`."""
    if plaintext is None or plaintext == "":
        return plaintext
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return _PREFIX + token


def decrypt(ciphertext: str | None) -> str | None:
    """Decrypt a value produced by `encrypt`. Plaintext/legacy values pass through."""
    if ciphertext is None or ciphertext == "":
        return ciphertext
    if not ciphertext.startswith(_PREFIX):
        # Not encrypted (e.g. set directly); return as-is.
        return ciphertext
    raw = ciphertext[len(_PREFIX):]
    try:
        return _fernet().decrypt(raw.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return None


def mask(secret: str | None, visible: int = 4) -> str:
    """Mask a secret for display/logging (keeps the last few chars)."""
    if not secret:
        return ""
    plain = decrypt(secret) or ""
    if len(plain) <= visible:
        return "•" * len(plain)
    return "•" * (len(plain) - visible) + plain[-visible:]
