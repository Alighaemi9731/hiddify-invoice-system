"""
Login security primitives: rate-limiting, a built-in (no external service) captcha,
and TOTP helpers for 2FA.

State is in-process (a single backend instance). For multi-instance later, back the
attempt counters + captcha store with Redis — the function signatures stay the same.
"""
from __future__ import annotations

import base64
import io
import secrets
import time
from dataclasses import dataclass, field

import pyotp

# ----------------------------- rate limiting -----------------------------
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 15 * 60   # attempts counted within this window
LOCKOUT_SECONDS = 15 * 60  # how long a key stays locked after hitting the cap


@dataclass
class _Bucket:
    fails: list[float] = field(default_factory=list)
    locked_until: float = 0.0


_buckets: dict[str, _Bucket] = {}


def _key(username: str, ip: str) -> str:
    return f"{(username or '').lower()}|{ip or '?'}"


def is_locked(username: str, ip: str) -> int:
    """Return remaining lockout seconds (0 if not locked)."""
    b = _buckets.get(_key(username, ip))
    if not b:
        return 0
    remaining = int(b.locked_until - time.time())
    return max(0, remaining)


def record_failure(username: str, ip: str) -> int:
    """Record a failed attempt; lock the key if the cap is reached.
    Returns remaining attempts before lockout (0 means now locked)."""
    now = time.time()
    b = _buckets.setdefault(_key(username, ip), _Bucket())
    b.fails = [t for t in b.fails if now - t < WINDOW_SECONDS]
    b.fails.append(now)
    if len(b.fails) >= MAX_ATTEMPTS:
        b.locked_until = now + LOCKOUT_SECONDS
        b.fails.clear()
        return 0
    return MAX_ATTEMPTS - len(b.fails)


def reset(username: str, ip: str) -> None:
    _buckets.pop(_key(username, ip), None)


# ----------------------------- captcha -----------------------------
# A lightweight, self-contained captcha: a short code rendered to a noisy PNG.
# The answer is kept server-side keyed by an opaque id with a short TTL.
_captchas: dict[str, tuple[str, float]] = {}
_CAPTCHA_TTL = 5 * 60
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no easily-confused chars


def _gc_captchas() -> None:
    now = time.time()
    for cid in [c for c, (_, exp) in _captchas.items() if exp < now]:
        _captchas.pop(cid, None)


def new_captcha() -> tuple[str, str]:
    """Create a captcha. Returns (captcha_id, data_uri_png)."""
    _gc_captchas()
    code = "".join(secrets.choice(_ALPHABET) for _ in range(5))
    cid = secrets.token_urlsafe(16)
    _captchas[cid] = (code, time.time() + _CAPTCHA_TTL)
    return cid, _render_png(code)


def verify_captcha(captcha_id: str, answer: str) -> bool:
    _gc_captchas()
    item = _captchas.pop(captcha_id or "", None)  # single-use
    if not item:
        return False
    code, exp = item
    if exp < time.time():
        return False
    return (answer or "").strip().upper() == code


def _captcha_font(size: int):
    """A TrueType font that exists on the server. Uses the bundled DejaVuSans
    (shipped with the app for PDFs) and never the macOS-only path."""
    from pathlib import Path

    from PIL import ImageFont

    candidates = [
        Path(__file__).resolve().parents[1] / "assets" / "fonts" / "DejaVuSans.ttf",
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for p in candidates:
        try:
            if p.exists():
                return ImageFont.truetype(str(p), size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def _render_png(code: str) -> str:
    """Render the code to a noisy PNG, returned as a data: URI."""
    from PIL import Image, ImageDraw

    w, h = 180, 60
    img = Image.new("RGB", (w, h), (240, 243, 250))
    d = ImageDraw.Draw(img)
    # speckle noise
    for _ in range(420):
        d.point((secrets.randbelow(w), secrets.randbelow(h)),
                fill=(secrets.randbelow(200) + 30,) * 3)
    for _ in range(5):  # a few distractor lines
        d.line((secrets.randbelow(w), secrets.randbelow(h),
                secrets.randbelow(w), secrets.randbelow(h)),
               fill=(150, 160, 180), width=1)
    font = _captcha_font(34)
    x = 18
    for ch in code:
        y = 8 + secrets.randbelow(8)
        d.text((x, y), ch, font=font, fill=(31, 59, 115))
        x += 30
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# ----------------------------- TOTP / 2FA -----------------------------
def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str, issuer: str = "Invoice Panel") -> str:
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str) -> bool:
    if not secret or not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(str(code).strip(), valid_window=1)
    except Exception:  # noqa: BLE001
        return False


def totp_qr_data_uri(uri: str) -> str:
    import qrcode

    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
