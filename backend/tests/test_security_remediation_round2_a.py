"""Security remediation — Round 2, Batch A (front door).

F1 (Critical): the bootstrap token is consumed only in the SAME commit as owner creation, so a
    validation failure never burns the token (closes the consume-then-fail setup takeover). Legacy
    no-token installs may only be set up from loopback.
F2 (Strict): a credential OR bearer request over plaintext HTTP from a non-loopback client is refused
    ALWAYS (even before HTTPS is configured); loopback + real HTTPS are allowed.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/secremr2a.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.security import hash_password  # noqa: E402
from app.models.app_user import AppUser  # noqa: E402
from app.services import settings_service  # noqa: E402
from tests.pg_barrier import make_engine, requires_pg  # noqa: E402


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


def _req(host="127.0.0.1", proto="http"):
    return SimpleNamespace(
        headers={"x-forwarded-proto": proto} if proto else {},
        url=SimpleNamespace(scheme="http"),
        client=SimpleNamespace(host=host),
    )


# ───────────────────────── F1: atomic setup / token intact on failure ─────────────────────────
def test_f1_bad_password_keeps_token_and_no_owner(tmp_path):
    """The exact residual: a correct token + an INVALID password must 400 WITHOUT consuming the token
    and WITHOUT creating an owner — and a subsequent tokenless retry must still be refused."""
    async def body(s, _S):
        from app.api import setup as setup_mod
        await settings_service.set_value(s, "setup_bootstrap_token_hash", hash_password("tok-secret"))
        await settings_service.set_value(s, "setup_done", False)
        await s.commit()
        req = _req()  # loopback
        orig_hash = await settings_service.get(s, "setup_bootstrap_token_hash", "")
        assert orig_hash  # sanity: a token hash is stored

        # correct token, but the password fails validation → 400
        with pytest.raises(HTTPException) as ei:
            await setup_mod.do_setup(
                setup_mod.SetupRequest(username="owner", password="short", token="tok-secret"),
                req, session=s)
        assert ei.value.status_code == 400
        # token still intact (unchanged), no owner created
        assert (await settings_service.get(s, "setup_bootstrap_token_hash", "")) == orig_hash
        assert (await s.execute(select(func.count(AppUser.id)))).scalar_one() == 0

        # a follow-up request with NO token must still be rejected (the token gate is intact)
        with pytest.raises(HTTPException) as ei2:
            await setup_mod.do_setup(
                setup_mod.SetupRequest(username="owner", password="password123", token=None),
                req, session=s)
        assert ei2.value.status_code == 403
        assert (await s.execute(select(func.count(AppUser.id)))).scalar_one() == 0

        # finally, the correct token + a valid password succeeds and consumes the token
        r = await setup_mod.do_setup(
            setup_mod.SetupRequest(username="owner", password="password123", token="tok-secret"),
            req, session=s)
        assert r["setup_done"] is True
        assert (await s.execute(select(func.count(AppUser.id)))).scalar_one() == 1
        assert (await settings_service.get(s, "setup_bootstrap_token_hash", "")) == ""
    _run(body, tmp_path, "f1keep.db")


def test_f1_no_token_nonloopback_refused(tmp_path):
    """Fail-closed: a legacy install with no minted token cannot be set up by an anonymous public
    (non-loopback) request even over HTTPS — 403. (A plaintext non-loopback request is refused a step
    earlier by F2's 426.) Loopback (SSH tunnel) is still allowed (covered by b5)."""
    async def body(s, _S):
        from app.api import setup as setup_mod
        await settings_service.set_value(s, "setup_done", False)
        await s.commit()
        # HTTPS (so F2 passes), non-loopback, no token stored → F1 fail-closed 403
        with pytest.raises(HTTPException) as ei:
            await setup_mod.do_setup(
                setup_mod.SetupRequest(username="owner", password="password123"),
                _req(host="1.2.3.4", proto="https"), session=s)
        assert ei.value.status_code == 403
        assert (await s.execute(select(func.count(AppUser.id)))).scalar_one() == 0
    _run(body, tmp_path, "f1closed.db")


# ───────────────────────── F2: Strict transport gate ─────────────────────────
def test_f2_require_secure_transport():
    from app.core.security import require_secure_transport
    # real HTTPS (Caddy sets the forwarded proto) and loopback are allowed
    require_secure_transport(_req(host="1.2.3.4", proto="https"))
    require_secure_transport(_req(host="127.0.0.1", proto="http"))
    require_secure_transport(_req(host="::1", proto="http"))
    # plaintext from a non-loopback client → 426 (always, even with no HTTPS configured)
    with pytest.raises(HTTPException) as ei:
        require_secure_transport(_req(host="1.2.3.4", proto="http"))
    assert ei.value.status_code == 426


def test_f2_get_current_subject_transport_gated(tmp_path):
    """Every bearer request is transport-gated: a plaintext non-loopback request is refused (426)
    BEFORE the token is even decoded; a loopback request proceeds to the normal 401 on a bad token."""
    async def body(s, _S):
        from app.core.security import get_current_subject
        # plaintext non-loopback → 426 regardless of the (garbage) token
        with pytest.raises(HTTPException) as ei:
            await get_current_subject(token="garbage", session=s, request=_req(host="1.2.3.4"))
        assert ei.value.status_code == 426
        # loopback → transport passes, garbage token → 401
        with pytest.raises(HTTPException) as ei2:
            await get_current_subject(token="garbage", session=s, request=_req(host="127.0.0.1"))
        assert ei2.value.status_code == 401
        # no request (direct/internal call) → transport check skipped, 401 on the garbage token
        with pytest.raises(HTTPException) as ei3:
            await get_current_subject(token="garbage", session=s)
        assert ei3.value.status_code == 401
    _run(body, tmp_path, "f2gcs.db")


# ───────────────────────── F1: PG setup race → exactly one owner ─────────────────────────
@pytest.mark.pg_contract
@requires_pg
def test_f1_setup_race_one_owner(monkeypatch):
    """Two connections race /api/setup with the SAME valid token. The `SELECT setup_done FOR UPDATE`
    (held to commit) must serialize them so exactly ONE owner is created and the loser gets 409 — even
    with the intra-process asyncio lock disabled (this is the cross-process DB guarantee)."""
    async def run():
        from app.api import setup as setup_mod

        class _NoLock:  # disable the intra-process lock so the DB row lock is what serializes
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(setup_mod, "_setup_lock", _NoLock())
        engine, factory = make_engine()
        try:
            async with factory() as s:
                await settings_service.set_value(
                    s, "setup_bootstrap_token_hash", hash_password("race-tok"))
                await settings_service.set_value(s, "setup_done", False)
                await s.commit()

            async def _do():
                async with factory() as s:
                    return await setup_mod.do_setup(
                        setup_mod.SetupRequest(username="owner", password="password123", token="race-tok"),
                        _req(), session=s)

            r1, r2 = await asyncio.gather(_do(), _do(), return_exceptions=True)
            oks = [r for r in (r1, r2) if isinstance(r, dict)]
            conflicts = [r for r in (r1, r2) if isinstance(r, HTTPException) and r.status_code == 409]
            assert len(oks) == 1, (r1, r2)
            assert len(conflicts) == 1, (r1, r2)

            async with factory() as s:
                n = (await s.execute(select(func.count(AppUser.id))
                     .where(AppUser.username == "owner"))).scalar_one()
                assert n == 1
                # cleanup
                await s.execute(AppUser.__table__.delete().where(AppUser.username == "owner"))
                from app.models.setting import Setting
                await s.execute(Setting.__table__.delete().where(
                    Setting.key.in_(("setup_bootstrap_token_hash", "setup_done"))))
                await s.commit()
        finally:
            await engine.dispose()
    asyncio.run(run())
