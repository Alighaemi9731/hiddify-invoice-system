"""Security contract for the PERMANENT portal address `/portal/u/{uuid}`.

The old portal link was a 15-minute one-time token, so a link tapped a few hours later (from the
bot menu, or a «مشاهده در پنل» support notification) just failed. The address is now permanent —
which is only safe because the uuid is an ADDRESS, never a credential:

  * opening the URL grants nothing by itself;
  * access requires PROVING which Telegram account is asking (Telegram Login Widget HMAC);
  * and that proven account must actually own the reseller the uuid points at.

So pasting somebody else's uuid can never open their portal. These tests pin exactly that.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base, get_session
from app.core.portal_auth import verify_telegram_login
from app.main import app
from app.models import Panel, Reseller

BOT_TOKEN = "123456:test-bot-token"


def _signed(payload: dict, token: str = BOT_TOKEN) -> dict:
    """Build a payload signed exactly the way Telegram's Login Widget signs it."""
    check = "\n".join(sorted(f"{k}={v}" for k, v in payload.items() if v is not None))
    secret = hashlib.sha256(token.encode()).digest()
    return {**payload, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()}


# ── unit: the signature verifier ──────────────────────────────────────────────
def test_verify_telegram_login_accepts_only_a_genuine_fresh_signature():
    good = _signed({"id": 777, "first_name": "A", "auth_date": int(time.time())})
    assert verify_telegram_login(good, BOT_TOKEN) == 777

    # Tampering with ANY field invalidates the signature (here: claim to be someone else).
    forged = {**good, "id": 999}
    assert verify_telegram_login(forged, BOT_TOKEN) is None

    # A payload signed for a DIFFERENT bot must not authenticate against ours.
    other = _signed({"id": 777, "first_name": "A", "auth_date": int(time.time())}, "999:other")
    assert verify_telegram_login(other, BOT_TOKEN) is None

    # Replay of an old capture is refused.
    stale = _signed({"id": 777, "first_name": "A", "auth_date": int(time.time()) - 90000})
    assert verify_telegram_login(stale, BOT_TOKEN) is None

    # Junk / missing hash.
    assert verify_telegram_login({"id": 777}, BOT_TOKEN) is None
    assert verify_telegram_login({}, BOT_TOKEN) is None
    assert verify_telegram_login(good, "") is None


# ── HTTP: ownership is enforced on the stable link ────────────────────────────
def _run(body):  # noqa: ANN001, ANN202
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                async def session_override():
                    yield session
                app.dependency_overrides[get_session] = session_override
                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    await body(session, client)
        finally:
            app.dependency_overrides.clear()
            await engine.dispose()

    asyncio.run(go())


async def _seed(session):  # noqa: ANN001, ANN202
    from app.services import settings_service

    panel = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
    session.add(panel)
    await session.flush()
    session.add_all([
        Reseller(panel_id=panel.id, admin_uuid="uuid-alice", name="Alice", bot_chat_id=111),
        Reseller(panel_id=panel.id, admin_uuid="uuid-bob", name="Bob", bot_chat_id=222),
    ])
    await settings_service.set_value(session, "telegram_bot_token", BOT_TOKEN)
    await session.commit()


def test_stable_link_lets_the_owner_in_and_refuses_everybody_else():
    async def body(session, client):  # noqa: ANN001
        await _seed(session)
        fresh = lambda uid: _signed(  # noqa: E731
            {"id": uid, "first_name": "X", "auth_date": int(time.time())})

        # Alice opening HER OWN address → a session is issued.
        ok = await client.post("/api/portal/auth/telegram",
                               json={"uuid": "uuid-alice", "auth": fresh(111)})
        assert ok.status_code == 200, ok.text
        assert ok.json()["access_token"]

        # THE ATTACK: Bob authenticates honestly as himself but pastes ALICE's uuid → refused.
        stolen = await client.post("/api/portal/auth/telegram",
                                   json={"uuid": "uuid-alice", "auth": fresh(222)})
        assert stolen.status_code == 403

        # A forged/unsigned payload for Alice's uuid → refused (401, never a session).
        forged = await client.post(
            "/api/portal/auth/telegram",
            json={"uuid": "uuid-alice", "auth": {"id": 111, "auth_date": int(time.time()),
                                                 "hash": "deadbeef"}})
        assert forged.status_code == 401

        # An unknown uuid is refused with the SAME response as a foreign one (no existence probe).
        unknown = await client.post("/api/portal/auth/telegram",
                                    json={"uuid": "uuid-nobody", "auth": fresh(111)})
        assert unknown.status_code == 403
        assert unknown.json()["detail"] == stolen.json()["detail"]

    _run(body)


def test_entry_config_leaks_nothing_about_who_owns_a_uuid():
    """The pre-login page must not reveal whether a uuid exists or whose it is — otherwise the
    permanent address becomes an enumeration oracle for reseller identities."""
    async def body(session, client):  # noqa: ANN001
        await _seed(session)
        real = await client.get("/api/portal/auth/entry", params={"uuid": "uuid-alice"})
        fake = await client.get("/api/portal/auth/entry", params={"uuid": "uuid-nobody"})
        assert real.status_code == fake.status_code == 200
        assert real.json() == fake.json()          # byte-identical: no existence/identity leak
        assert "Alice" not in real.text

    _run(body)


async def _seed_duplicate_uuid(session, *, bob_first: bool):  # noqa: ANN001, ANN202
    """The SAME admin_uuid on two panels, owned by two different Telegram accounts.

    Legitimate and expected: `uq_reseller_panel_uuid` makes admin_uuid unique per PANEL, not
    globally, and two Hiddify panels can independently hand out the same uuid. `bob_first` varies
    only the INSERT order, which is the whole point — the outcome must not depend on it.
    """
    from app.services import settings_service

    p1 = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
    p2 = Panel(key="p2", host="p2.invalid", proxy_path_enc="x", owner_uuid="o")
    session.add_all([p1, p2])
    await session.flush()
    alice = Reseller(panel_id=p1.id, admin_uuid="shared-uuid", name="Alice", bot_chat_id=111)
    bob = Reseller(panel_id=p2.id, admin_uuid="shared-uuid", name="Bob", bot_chat_id=222)
    session.add_all([bob, alice] if bob_first else [alice, bob])
    await settings_service.set_value(session, "telegram_bot_token", BOT_TOKEN)
    await session.commit()


def test_duplicate_uuid_on_two_panels_lets_each_real_owner_in():
    """Both owners of a shared uuid must authenticate, regardless of DB row order.

    The endpoint used to pick an arbitrary `.limit(1)` uuid match and only THEN compare
    bot_chat_id, so exactly one of the two owners was refused entry to their own portal — chosen
    by undefined row order, and able to flip whenever Postgres rewrote the winning row.
    """
    for bob_first in (False, True):
        def body(bob_first=bob_first):
            async def inner(session, client):  # noqa: ANN001
                await _seed_duplicate_uuid(session, bob_first=bob_first)
                fresh = lambda uid: _signed(  # noqa: E731
                    {"id": uid, "first_name": "X", "auth_date": int(time.time())})

                for chat_id in (111, 222):
                    resp = await client.post(
                        "/api/portal/auth/telegram",
                        json={"uuid": "shared-uuid", "auth": fresh(chat_id)})
                    assert resp.status_code == 200, (
                        f"owner {chat_id} locked out of their own portal "
                        f"(insert order bob_first={bob_first}): {resp.text}"
                    )
                    assert resp.json()["access_token"]

                # A third party who proves a DIFFERENT Telegram id is still refused.
                intruder = await client.post(
                    "/api/portal/auth/telegram",
                    json={"uuid": "shared-uuid", "auth": fresh(999)})
                assert intruder.status_code == 403
            return inner
        _run(body())


def test_duplicate_uuid_match_stays_case_insensitive():
    async def body(session, client):  # noqa: ANN001
        await _seed_duplicate_uuid(session, bob_first=False)
        resp = await client.post(
            "/api/portal/auth/telegram",
            json={"uuid": "SHARED-UUID",
                  "auth": _signed({"id": 222, "first_name": "X",
                                   "auth_date": int(time.time())})})
        assert resp.status_code == 200, resp.text
    _run(body)
