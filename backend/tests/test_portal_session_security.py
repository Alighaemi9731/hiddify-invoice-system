"""Portal sessions must be revocable, and a Telegram login payload must be single-use.

Two defects, both of which turned a convenience into an indefinite grant:

  * The session token was a stateless 30-day JWT that SLIDES — the client trades a valid one for a
    fresh one while in use — and the only liveness check was "does *some* reseller row still carry
    this bot_chat_id". So unbinding one panel of a multi-panel account revoked nothing, and there
    was no way at all to end a session early.
  * `verify_telegram_login` is pure: it checks the HMAC and the payload age but CONSUMES nothing.
    One captured sign-in body could therefore be replayed for the whole freshness window, each
    replay minting another fresh 30-day sliding session — so a single capture became permanent
    access.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/portalsec.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base, get_session  # noqa: E402
from app.core.portal_auth import (  # noqa: E402
    bump_session_epoch,
    create_portal_session_token,
    current_session_epoch,
    get_current_reseller,
)
from app.main import app  # noqa: E402
from app.models import Panel, PortalSessionEpoch, Reseller  # noqa: E402

BOT_TOKEN = "123456:TESTTOKEN"


def _signed(payload: dict) -> dict:
    data = {k: v for k, v in payload.items() if k != "hash"}
    check = "\n".join(sorted(f"{k}={v}" for k, v in data.items() if v is not None))
    secret = hashlib.sha256(BOT_TOKEN.encode()).digest()
    return {**data, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()}


def _run(body):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                async def override():
                    yield session
                app.dependency_overrides[get_session] = override
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    await body(session, client)
        finally:
            app.dependency_overrides.clear()
            await engine.dispose()

    asyncio.run(go())


async def _seed(session, *, panels: int = 1, chat_id: int = 111):
    from app.services import settings_service

    for i in range(panels):
        panel = Panel(key=f"p{i}", host=f"p{i}.invalid", proxy_path_enc="x", owner_uuid="o")
        session.add(panel)
        await session.flush()
        session.add(Reseller(panel_id=panel.id, admin_uuid="shared-uuid",
                             name=f"R{i}", bot_chat_id=chat_id))
    await settings_service.set_value(session, "telegram_bot_token", BOT_TOKEN)
    await session.commit()


def test_a_session_survives_normal_use():
    async def body(session, _client):
        await _seed(session)
        token = create_portal_session_token(111, await current_session_epoch(session, 111))
        ctx = await get_current_reseller(token, session)
        assert ctx.chat_id == 111

    _run(body)


def test_a_token_minted_before_the_epoch_existed_still_authenticates():
    """BACKWARD COMPATIBILITY. Every live session predates this claim; if a missing `epoch` were
    rejected, deploying the fix would log every reseller out — a worse outcome than the bug."""
    from app.core.security import create_access_token

    async def body(session, _client):
        await _seed(session)
        legacy = create_access_token("111", {"role": "reseller"})   # no epoch claim at all
        ctx = await get_current_reseller(legacy, session)
        assert ctx.chat_id == 111

    _run(body)


def test_unbinding_one_panel_revokes_the_session_across_sibling_rows():
    """The actual bug: `get_current_reseller` only needs SOME row to carry the chat id, so on a
    multi-panel account unbinding one panel left the person fully signed in — including to the
    panel they were just unbound from, via their other rows."""
    async def body(session, _client):
        from app.api import resellers as resellers_api

        await _seed(session, panels=2)
        token = create_portal_session_token(111, await current_session_epoch(session, 111))
        assert (await get_current_reseller(token, session)).chat_id == 111

        rows = (await session.execute(
            __import__("sqlalchemy").select(Reseller))).scalars().all()
        await resellers_api.unbind_telegram(rows[0].id, session=session)

        # The other row still carries the chat id, so the OLD check would still have let them in.
        with pytest.raises(HTTPException):
            await get_current_reseller(token, session)

    _run(body)


def test_a_fresh_login_after_revocation_works_again():
    """Revocation must not brick the account — the next legitimate sign-in mints at the new epoch."""
    async def body(session, _client):
        await _seed(session)
        old = create_portal_session_token(111, await current_session_epoch(session, 111))
        await bump_session_epoch(session, 111)
        await session.commit()

        with pytest.raises(HTTPException):
            await get_current_reseller(old, session)

        fresh = create_portal_session_token(111, await current_session_epoch(session, 111))
        assert (await get_current_reseller(fresh, session)).chat_id == 111

    _run(body)


def test_the_sliding_refresh_chain_cannot_outlive_a_revocation():
    """Refresh re-mints at the CURRENT epoch, so a bump breaks the chain. Otherwise a revoked
    session could renew itself forever, which is what made the 30-day TTL unbounded in practice."""
    async def body(session, client):
        await _seed(session)
        token = create_portal_session_token(111, await current_session_epoch(session, 111))

        r1 = await client.post("/api/portal/auth/refresh",
                               headers={"Authorization": f"Bearer {token}"})
        assert r1.status_code == 200, r1.text
        renewed = r1.json()["access_token"]

        await bump_session_epoch(session, 111)
        await session.commit()

        r2 = await client.post("/api/portal/auth/refresh",
                               headers={"Authorization": f"Bearer {renewed}"})
        assert r2.status_code == 401, "a revoked session renewed itself"

    _run(body)


def test_bumping_is_monotonic_and_starts_from_the_implicit_one():
    async def body(session, _client):
        await _seed(session)
        assert await current_session_epoch(session, 111) == 1     # no row yet
        await bump_session_epoch(session, 111)
        await session.commit()
        assert await current_session_epoch(session, 111) == 2
        await bump_session_epoch(session, 111)
        await session.commit()
        assert await current_session_epoch(session, 111) == 3
        assert await session.get(PortalSessionEpoch, 111) is not None

    _run(body)


def test_bumping_an_unbound_account_is_a_no_op():
    async def body(session, _client):
        await _seed(session)
        await bump_session_epoch(session, None)     # reseller was never registered
        await session.commit()
        assert await current_session_epoch(session, 111) == 1

    _run(body)


# ── Telegram login replay ─────────────────────────────────────────────────────────────────────
def test_the_same_telegram_payload_cannot_be_used_twice():
    """A captured sign-in body was replayable for the whole freshness window, each replay minting
    another fresh 30-day sliding session — one capture became permanent access."""
    async def body(session, client):
        await _seed(session)
        auth = _signed({"id": 111, "first_name": "X", "auth_date": int(time.time())})

        first = await client.post("/api/portal/auth/telegram",
                                  json={"uuid": "shared-uuid", "auth": auth})
        assert first.status_code == 200, first.text
        assert first.json()["access_token"]

        replay = await client.post("/api/portal/auth/telegram",
                                   json={"uuid": "shared-uuid", "auth": auth})
        assert replay.status_code == 401, "the identical signed payload was accepted twice"

    _run(body)


def test_a_second_genuine_login_still_works():
    """Consumption must bind to the exact payload, not lock the account out of signing in again."""
    async def body(session, client):
        await _seed(session)
        a1 = _signed({"id": 111, "first_name": "X", "auth_date": int(time.time())})
        a2 = _signed({"id": 111, "first_name": "X", "auth_date": int(time.time()) - 5})
        assert a1["hash"] != a2["hash"]

        assert (await client.post("/api/portal/auth/telegram",
                                  json={"uuid": "shared-uuid", "auth": a1})).status_code == 200
        assert (await client.post("/api/portal/auth/telegram",
                                  json={"uuid": "shared-uuid", "auth": a2})).status_code == 200

    _run(body)


def test_a_stale_payload_is_refused_well_inside_a_day():
    """The window was a full DAY for a signed credential. 15 minutes is still generous for a widget
    that is used the instant it is tapped."""
    from app.core import portal_auth

    async def body(session, client):
        await _seed(session)
        assert portal_auth.TELEGRAM_LOGIN_MAX_AGE_S <= 15 * 60
        stale = _signed({"id": 111, "first_name": "X",
                         "auth_date": int(time.time()) - 3600})     # an hour old
        resp = await client.post("/api/portal/auth/telegram",
                                 json={"uuid": "shared-uuid", "auth": stale})
        assert resp.status_code == 401

    _run(body)


def test_a_forged_payload_is_still_refused_and_consumes_nothing():
    async def body(session, client):
        await _seed(session)
        forged = {"id": 111, "first_name": "X", "auth_date": int(time.time()), "hash": "de" * 32}
        assert (await client.post("/api/portal/auth/telegram",
                                  json={"uuid": "shared-uuid", "auth": forged})).status_code == 401
        # …and the genuine payload for the same account still works afterwards.
        good = _signed({"id": 111, "first_name": "X", "auth_date": int(time.time())})
        assert (await client.post("/api/portal/auth/telegram",
                                  json={"uuid": "shared-uuid", "auth": good})).status_code == 200

    _run(body)
