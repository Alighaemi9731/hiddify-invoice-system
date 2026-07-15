"""Security remediation — Batch 5 (front-door & auth hardening).

F1 first-run /api/setup requires the installer's one-time bootstrap token (when configured).
F2 a credential request over plaintext HTTP from a non-loopback client is refused once HTTPS is on.
F8 the unauthenticated passkey login-begin is per-IP throttled and the challenge store is bounded.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/secremb5.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.models.app_user import AppUser  # noqa: E402
from app.services import settings_service  # noqa: E402


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        S = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with S() as s:
                await coro_fn(s, S)
        finally:
            await engine.dispose()
    asyncio.run(go())


def _loopback_req():
    return SimpleNamespace(headers={}, url=SimpleNamespace(scheme="http"),
                           client=SimpleNamespace(host="127.0.0.1"))


# ───────────────────────── F1: setup bootstrap token ─────────────────────────
def test_f1_setup_requires_bootstrap_token(tmp_path):
    async def body(s, _S):
        from app.api import setup as setup_mod
        await settings_service.set_value(s, "setup_bootstrap_token_hash", hash_password("tok-secret"))
        await settings_service.set_value(s, "setup_done", False)
        await s.commit()
        req = _loopback_req()

        # status advertises that a token is required
        st = await setup_mod.status(session=s)
        assert st.token_required is True

        # missing / wrong token → 403, no owner created
        for bad in (None, "wrong"):
            with pytest.raises(HTTPException) as ei:
                await setup_mod.do_setup(
                    setup_mod.SetupRequest(username="owner", password="password123", token=bad),
                    req, session=s)
            assert ei.value.status_code == 403
        assert (await s.execute(select(func.count(AppUser.id)))).scalar_one() == 0

        # correct token → owner created + token consumed (one-time)
        r = await setup_mod.do_setup(
            setup_mod.SetupRequest(username="owner", password="password123", token="tok-secret"),
            req, session=s)
        assert r["setup_done"] is True
        assert (await s.execute(select(func.count(AppUser.id)))).scalar_one() == 1
        assert (await settings_service.get(s, "setup_bootstrap_token_hash", "")) == ""
    _run(body, tmp_path, "f1.db")


def test_f1_setup_open_without_token_when_none_configured(tmp_path):
    async def body(s, _S):
        from app.api import setup as setup_mod
        await settings_service.set_value(s, "setup_done", False)
        await s.commit()
        st = await setup_mod.status(session=s)
        assert st.token_required is False   # no hash stored → no token gate (backward compat)
        r = await setup_mod.do_setup(
            setup_mod.SetupRequest(username="owner", password="password123"),
            _loopback_req(), session=s)
        assert r["setup_done"] is True
    _run(body, tmp_path, "f1open.db")


# ───────────────────────── F2: HTTPS-only credentials ─────────────────────────
def test_f2_secure_or_loopback_ok():
    from app.core.loginsec import secure_or_loopback_ok
    assert secure_or_loopback_ok("https", "1.2.3.4", True) is True    # real HTTPS (via Caddy)
    assert secure_or_loopback_ok("http", "127.0.0.1", True) is True   # loopback (SSH tunnel)
    assert secure_or_loopback_ok("http", "::1", True) is True         # loopback v6
    assert secure_or_loopback_ok("http", "1.2.3.4", False) is True    # bare-IP setup phase (no HTTPS yet)
    assert secure_or_loopback_ok("http", "1.2.3.4", True) is False    # plaintext in production → refused


# ───────────────────────── F8: passkey throttle + store cap ─────────────────────────
def test_f8_passkey_login_throttle():
    from app.core import loginsec
    ip = "203.0.113.77"   # fresh ip
    allowed = sum(1 for _ in range(loginsec._PASSKEY_MAX_PER_WINDOW + 10)
                  if loginsec.passkey_login_allowed(ip))
    assert allowed == loginsec._PASSKEY_MAX_PER_WINDOW   # burst then refused


def test_f8_webauthn_store_bounded():
    from app.core import webauthn_store
    webauthn_store._store.clear()
    for _ in range(webauthn_store._MAX_ENTRIES + 500):
        webauthn_store.put(b"a-challenge-bytes")
    assert len(webauthn_store._store) <= webauthn_store._MAX_ENTRIES
    webauthn_store._store.clear()
