"""Plan 004 HTTP contracts: tenant-safe customer list/detail/ledger + audited ban.

Covers keyset pagination + cursor tamper/wrong-endpoint (422), two-shop isolation (foreign
customer → 404), LTV from the confirmed ledger, ledger redaction, and idempotent ban that does
NOT bump config_version (so it never conflicts with a concurrent settings edit)."""
from __future__ import annotations

import asyncio
import datetime as dt

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base, get_session
from app.core.portal_auth import ResellerContext, get_current_reseller
from app.main import app
from app.models import (
    Panel,
    Reseller,
    StorefrontApiCommand,
    StorefrontAuditEvent,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
    StorefrontWalletTxn,
)

UTC = dt.timezone.utc


def _run(body):  # noqa: ANN001, ANN202
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                await body(session, factory)
        finally:
            app.dependency_overrides.clear()
            await engine.dispose()

    asyncio.run(go())


async def _seed(session):  # noqa: ANN001, ANN202
    panel = Panel(key="p1", name="P1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
    session.add(panel)
    await session.flush()
    owner = Reseller(panel_id=panel.id, admin_uuid="owner", name="Owner",
                     bot_chat_id=111, storefront_enabled=True)
    foreign = Reseller(panel_id=panel.id, admin_uuid="foreign", name="Foreign",
                       bot_chat_id=222, storefront_enabled=True)
    session.add_all([owner, foreign])
    await session.flush()
    shop = StorefrontBot(reseller_id=owner.id, panel_id=panel.id, bot_token_enc="t",
                         config_version=1)
    other = StorefrontBot(reseller_id=foreign.id, panel_id=panel.id, bot_token_enc="t2",
                          config_version=1)
    session.add_all([shop, other])
    await session.flush()

    def cust(sf, tid, name, *, banned=False, seen_days=0, created):  # noqa: ANN001, ANN202
        return StorefrontCustomer(
            storefront_bot_id=sf.id, telegram_id=tid, name=name, banned=banned,
            last_seen_at=dt.datetime.now(UTC) - dt.timedelta(days=seen_days),
            created_at=created)

    a = cust(shop, 501, "Alice", seen_days=0, created=dt.datetime(2026, 7, 1, tzinfo=UTC))
    b = cust(shop, 502, "Bob", banned=True, seen_days=40, created=dt.datetime(2026, 7, 2, tzinfo=UTC))
    c = cust(other, 503, "Carol", seen_days=0, created=dt.datetime(2026, 7, 3, tzinfo=UTC))
    session.add_all([a, b, c])
    await session.flush()

    # Alice has a provisioned service; Bob has none.
    session.add(StorefrontOrder(
        customer_id=a.id, panel_id=panel.id, panel_user_uuid="uuid-a", status="provisioned",
        gb=10, days=30, price_toman=50_000, created_at=dt.datetime(2026, 7, 1, 1, tzinfo=UTC)))
    # Ledger for Alice → LTV = -(−100000 + 20000 + 5000) = 75000. topup/credit_bonus/pending excluded.
    for kind, amt, status in [
        ("purchase", -100_000, "done"), ("refund", 20_000, "done"),
        ("renew_reversal", 5_000, "done"), ("topup", 50_000, "done"),
        ("credit_bonus", 10_000, "done"), ("purchase", -30_000, "pending"),
    ]:
        session.add(StorefrontWalletTxn(
            customer_id=a.id, storefront_bot_id=a.storefront_bot_id, kind=kind,
            amount_toman=amt, status=status,
            proof_path="/secret/proof.jpg", created_at=dt.datetime(2026, 7, 1, 2, tzinfo=UTC)))
    await session.commit()
    return owner, shop, other, a, b, c


def _client(session, owner):  # noqa: ANN001, ANN202
    async def session_override():
        yield session

    async def context_override():
        return ResellerContext(chat_id=111, resellers=[owner])

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_current_reseller] = context_override
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def test_customer_list_filters_keyset_isolation_and_cursor_guard():
    async def body(session, factory):  # noqa: ANN001
        owner, shop, other, a, b, c = await _seed(session)
        async with _client(session, owner) as client:
            base = f"/api/portal/storefronts/{shop.id}/customers"
            # All of this shop's customers (newest first), never the other shop's Carol.
            r = await client.get(base)
            assert r.status_code == 200
            names = [row["name"] for row in r.json()["items"]]
            assert names == ["Bob", "Alice"] and "Carol" not in names
            # banned filter
            assert [x["name"] for x in (await client.get(base, params={"banned": True})).json()["items"]] == ["Bob"]
            # activity filter (Alice active, Bob inactive)
            assert [x["name"] for x in (await client.get(base, params={"activity": "active30"})).json()["items"]] == ["Alice"]
            # has_service (only Alice has a provisioned order)
            assert [x["name"] for x in (await client.get(base, params={"has_service": True})).json()["items"]] == ["Alice"]
            # name search needs >= 3 chars
            assert (await client.get(base, params={"q": "Al"})).status_code == 422
            assert [x["name"] for x in (await client.get(base, params={"q": "Ali"})).json()["items"]] == ["Alice"]
            # exact telegram-id search
            assert [x["telegram_id"] for x in (await client.get(base, params={"q": "502"})).json()["items"]] == [502]

            # Keyset pagination: limit 1 → next_cursor, then the next page.
            p1 = (await client.get(base, params={"limit": 1})).json()
            assert [x["name"] for x in p1["items"]] == ["Bob"] and p1["next_cursor"]
            p2 = (await client.get(base, params={"limit": 1, "cursor": p1["next_cursor"]})).json()
            assert [x["name"] for x in p2["items"]] == ["Alice"]
            # Tampered + wrong-endpoint cursors → 422.
            assert (await client.get(base, params={"cursor": p1["next_cursor"] + "x"})).status_code == 422
            assert (await client.get(
                f"/api/portal/storefronts/{shop.id}/customers/{a.id}/orders",
                params={"cursor": p1["next_cursor"]})).status_code == 422

            # Foreign customer (other shop) → 404, never leaks.
            assert (await client.get(f"{base}/{c.id}")).status_code == 404

    _run(body)


def test_customer_list_cursor_is_scoped_to_its_own_shop():
    """The customer-list cursor tag must fold in shop_id like every sibling keyset list
    (orders/ledger by customer_id, topups by shop_id) — otherwise a cursor minted while paging one
    shop's customers still decodes successfully against a DIFFERENT shop's list. Not reachable as a
    cross-tenant leak (the WHERE clause always re-filters by the target shop_id), but one Telegram
    id can legitimately own several `Reseller` rows (one per panel — `StorefrontBot` is one-per-
    -reseller, per `uq_storefront_bot_reseller`), each with its own shop, all in the SAME portal
    session. A stray cursor pasted into the wrong one of ITS OWN shops must be refused, not silently
    mispositioned."""
    async def body(session, factory):  # noqa: ANN001
        owner, shop, _other, _a, _b, _c = await _seed(session)
        # A second reseller row for the SAME Telegram id (a different panel), with its own shop —
        # one portal session legitimately covering two shops.
        owner2 = Reseller(panel_id=shop.panel_id, admin_uuid="owner2", name="Owner2",
                          bot_chat_id=111, storefront_enabled=True)
        session.add(owner2)
        await session.flush()
        shop2 = StorefrontBot(reseller_id=owner2.id, panel_id=shop.panel_id, bot_token_enc="t3",
                              config_version=1)
        session.add(shop2)
        await session.flush()
        session.add(StorefrontCustomer(
            storefront_bot_id=shop2.id, telegram_id=901, name="Dave",
            created_at=dt.datetime(2026, 7, 4, tzinfo=UTC)))
        await session.commit()

        async def session_override():
            yield session

        async def context_override():
            return ResellerContext(chat_id=111, resellers=[owner, owner2])

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_reseller] = context_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            p1 = (await client.get(
                f"/api/portal/storefronts/{shop.id}/customers", params={"limit": 1})).json()
            assert p1["next_cursor"]
            cross_shop = await client.get(
                f"/api/portal/storefronts/{shop2.id}/customers",
                params={"cursor": p1["next_cursor"]})
            assert cross_shop.status_code == 422

    _run(body)


def test_customer_detail_ltv_ledger_redaction_and_no_sub_link():
    async def body(session, factory):  # noqa: ANN001
        owner, shop, other, a, b, c = await _seed(session)
        async with _client(session, owner) as client:
            detail = (await client.get(
                f"/api/portal/storefronts/{shop.id}/customers/{a.id}")).json()
            assert detail["net_ltv_toman"] == 75_000
            assert detail["service_counts"]["active"] == 1
            assert "sub_link" not in str(detail)

            ledger = (await client.get(
                f"/api/portal/storefronts/{shop.id}/customers/{a.id}/ledger")).json()
            assert "proof_path" not in str(ledger) and "/secret/" not in str(ledger)
            assert {row["kind"] for row in ledger["items"]}  # rows present
            # bad enum filters → 422
            assert (await client.get(
                f"/api/portal/storefronts/{shop.id}/customers/{a.id}/ledger",
                params={"kind": "bogus"})).status_code == 422

    _run(body)


def test_ban_is_idempotent_audited_and_does_not_bump_config_version():
    async def body(session, factory):  # noqa: ANN001
        owner, shop, other, a, b, c = await _seed(session)
        async with _client(session, owner) as client:
            url = f"/api/portal/storefronts/{shop.id}/customers/{a.id}/status"
            r = await client.patch(
                url, json={"banned": True, "reason": "spam abuse"},
                headers={"Idempotency-Key": "ban-1"})
            assert r.status_code == 200
            # config_version unchanged (entity command, no CAS) → still 1.
            assert r.headers["etag"] == '"sf-config-1"'
            assert (await session.get(StorefrontCustomer, a.id)).banned is True

            # Same key + same body → clean replay (still 200), one command row.
            again = await client.patch(
                url, json={"banned": True, "reason": "spam abuse"},
                headers={"Idempotency-Key": "ban-1"})
            assert again.status_code == 200
            # Same key + DIFFERENT body → 409 conflict.
            conflict = await client.patch(
                url, json={"banned": False, "reason": "changed mind here"},
                headers={"Idempotency-Key": "ban-1"})
            assert conflict.status_code == 409

            # reason too short → 422 (no state change).
            assert (await client.patch(
                url, json={"banned": False, "reason": "x"},
                headers={"Idempotency-Key": "ban-2"})).status_code == 422

            # audit event recorded; exactly one command for ban-1.
            events = (await session.execute(
                select(StorefrontAuditEvent).where(
                    StorefrontAuditEvent.action == "customer.ban"))).scalars().all()
            assert any(e.outcome == "succeeded" for e in events)
            cmds = (await session.execute(
                select(StorefrontApiCommand).where(
                    StorefrontApiCommand.idempotency_key == "ban-1"))).scalars().all()
            assert len(cmds) == 1

            # Foreign customer ban → 404.
            assert (await client.patch(
                f"/api/portal/storefronts/{shop.id}/customers/{c.id}/status",
                json={"banned": True, "reason": "nope nope"},
                headers={"Idempotency-Key": "ban-foreign"})).status_code == 404

    _run(body)
