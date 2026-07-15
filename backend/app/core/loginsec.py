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
MAX_ATTEMPTS = 5           # per (username, IP)
GLOBAL_MAX_ATTEMPTS = 30   # per username across ALL IPs — backstop for distributed attacks
WINDOW_SECONDS = 15 * 60   # attempts counted within this window
LOCKOUT_SECONDS = 15 * 60  # how long a key stays locked after hitting the cap


@dataclass
class _Bucket:
    fails: list[float] = field(default_factory=list)
    locked_until: float = 0.0


_buckets: dict[str, _Bucket] = {}
# Hard cap on the number of tracked buckets. Without eviction, an unauthenticated attacker
# POSTing a unique random username per request grows _buckets without bound until restart
# (each failed attempt — even a failed captcha — creates one). Evict elapsed buckets on
# insert, and if still over the cap drop the oldest-expiring ones.
_MAX_BUCKETS = 50_000


def _evict_buckets(now: float) -> None:
    """Drop buckets whose window AND lockout have both elapsed; if still over the cap, drop the
    entries closest to expiry. O(n) but only walks when inserting a NEW key."""
    dead = [
        k for k, b in _buckets.items()
        if b.locked_until < now and (not b.fails or now - max(b.fails) >= WINDOW_SECONDS)
    ]
    for k in dead:
        _buckets.pop(k, None)
    if len(_buckets) > _MAX_BUCKETS:
        # Keep the freshest (highest lockout / most recent failure); drop the rest.
        def _freshness(item: tuple[str, _Bucket]) -> float:
            b = item[1]
            return max(b.locked_until, max(b.fails) if b.fails else 0.0)

        for k, _b in sorted(_buckets.items(), key=_freshness)[: len(_buckets) - _MAX_BUCKETS]:
            _buckets.pop(k, None)


def _ip_key(username: str, ip: str) -> str:
    return f"ip|{(username or '').lower()}|{ip or '?'}"


def _user_key(username: str) -> str:
    return f"user|{(username or '').lower()}"


def is_locked(username: str, ip: str) -> int:
    """Return remaining lockout seconds (0 if not locked). Checks BOTH the per-IP bucket and
    the per-username global bucket so a spoofed/rotated IP can't reset the protection."""
    now = time.time()
    remaining = 0
    for k in (_ip_key(username, ip), _user_key(username)):
        b = _buckets.get(k)
        if b:
            remaining = max(remaining, int(b.locked_until - now))
    return max(0, remaining)


def record_failure(username: str, ip: str) -> int:
    """Record a failed attempt on both the per-IP and per-username buckets; lock whichever
    hits its cap. Returns the per-IP remaining attempts before lockout (0 = now locked)."""
    now = time.time()
    ip_remaining = MAX_ATTEMPTS
    _evict_buckets(now)  # bound the map before we may insert new keys
    for k, cap, is_ip in ((_ip_key(username, ip), MAX_ATTEMPTS, True),
                          (_user_key(username), GLOBAL_MAX_ATTEMPTS, False)):
        b = _buckets.setdefault(k, _Bucket())
        b.fails = [t for t in b.fails if now - t < WINDOW_SECONDS]
        b.fails.append(now)
        if len(b.fails) >= cap:
            b.locked_until = now + LOCKOUT_SECONDS
            b.fails.clear()
            if is_ip:
                ip_remaining = 0
        elif is_ip:
            ip_remaining = cap - len(b.fails)
    return ip_remaining


def reset(username: str, ip: str) -> None:
    # Only a successful login (correct password) reaches here, so clearing the global
    # username bucket can't be triggered by an attacker.
    _buckets.pop(_ip_key(username, ip), None)
    _buckets.pop(_user_key(username), None)


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


# Per-IP throttle for the UNAUTHENTICATED captcha endpoint: rendering a PNG (PIL, 280 speckle
# points) is a cheap CPU/memory amplifier if hammered. Allow a generous burst for a real
# login page, then reject.
_CAPTCHA_MAX_PER_WINDOW = 30
_CAPTCHA_WINDOW = 60.0
_captcha_hits: dict[str, list[float]] = {}


def captcha_allowed(ip: str) -> bool:
    """True if this IP may fetch another captcha now. Sliding-window; self-evicting."""
    now = time.time()
    # Opportunistic cleanup so this map can't grow without bound either.
    if len(_captcha_hits) > _MAX_BUCKETS:
        for k in [k for k, v in _captcha_hits.items() if not v or now - v[-1] > _CAPTCHA_WINDOW]:
            _captcha_hits.pop(k, None)
    hits = [t for t in _captcha_hits.get(ip or "?", []) if now - t < _CAPTCHA_WINDOW]
    if len(hits) >= _CAPTCHA_MAX_PER_WINDOW:
        _captcha_hits[ip or "?"] = hits
        return False
    hits.append(now)
    _captcha_hits[ip or "?"] = hits
    return True


# Per-IP throttle for the UNAUTHENTICATED passkey login-begin (F8): each begin generates WebAuthn
# options + a DB read and stashes a challenge; the completion-only lockout never fires on begins, so
# without this a flood could churn CPU/DB and grow the challenge store. Same sliding-window shape as
# the captcha throttle.
_PASSKEY_MAX_PER_WINDOW = 20
_PASSKEY_WINDOW = 60.0
_passkey_hits: dict[str, list[float]] = {}


def passkey_login_allowed(ip: str) -> bool:
    """True if this IP may start another passkey login now. Sliding-window; self-evicting."""
    now = time.time()
    if len(_passkey_hits) > _MAX_BUCKETS:
        for k in [k for k, v in _passkey_hits.items() if not v or now - v[-1] > _PASSKEY_WINDOW]:
            _passkey_hits.pop(k, None)
    hits = [t for t in _passkey_hits.get(ip or "?", []) if now - t < _PASSKEY_WINDOW]
    if len(hits) >= _PASSKEY_MAX_PER_WINDOW:
        _passkey_hits[ip or "?"] = hits
        return False
    hits.append(now)
    _passkey_hits[ip or "?"] = hits
    return True


def secure_or_loopback_ok(proto: str, client_ip: str, https_enabled: bool) -> bool:
    """F2: may a CREDENTIAL request proceed? Yes over real HTTPS (Caddy sets X-Forwarded-Proto),
    from a loopback caller (SSH-tunnel / health), or while HTTPS isn't configured yet (the bare-IP
    setup phase). Once a domain/HTTPS is on, a plaintext request from a non-loopback client is
    refused — a password/JWT must not ride cleartext."""
    if (proto or "").lower() == "https":
        return True
    if client_ip in ("127.0.0.1", "::1", "localhost", ""):
        return True
    return not https_enabled


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
    img = Image.new("RGB", (w, h), (247, 249, 252))
    d = ImageDraw.Draw(img)
    # speckle noise
    for _ in range(280):
        d.point((secrets.randbelow(w), secrets.randbelow(h)),
                fill=(secrets.randbelow(135) + 105,) * 3)
    for _ in range(4):  # a few distractor lines
        d.line((secrets.randbelow(w), secrets.randbelow(h),
                secrets.randbelow(w), secrets.randbelow(h)),
               fill=(170, 181, 198), width=1)
    font = _captcha_font(34)
    x = 18
    for ch in code:
        y = 8 + secrets.randbelow(8)
        d.text((x, y), ch, font=font, fill=(8, 35, 72), stroke_width=1,
               stroke_fill=(8, 35, 72))
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
    img.save(buf)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
