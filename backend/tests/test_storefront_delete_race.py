"""Deleting a service while a purchase or renewal is in flight must not cost the customer money.

`delete_subscription` only refused `deleted`/`failed` orders, so `pending` (mid-purchase) and
`renewing` (mid-renewal) went straight through — and both cases lose real money:

  * mid-PURCHASE the debit is already committed before the panel call. `purchase` then treats any
    non-`pending` status as "the reaper finalized this, and the reaper refunds" — so nothing
    refunded, while the bot told the customer «مبلغ به کیفِ پولِ شما بازگردانده شد».
  * mid-RENEWAL the panel config is destroyed and `renew`'s final write flips the order back to
    `provisioned` — resurrecting a service that no longer exists, which then shows in «سرویس‌های من»
    and gets charged again at the next renewal.

The invariant asserted here: an order leaves for `deleted` only from a settled state, under the
per-customer lock, with no live provisioning lease — and any charge for a service that ended up not
existing comes back exactly once.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/sfdelrace.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import (  # noqa: E402
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
    StorefrontPlan,
    StorefrontWalletTxn,
)
from app.services import (  # noqa: E402
    storefront_provision,
    storefront_subscription,
    storefront_wallet,
    usercreate,
)


def _run(body, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
        from app.core.db import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await body(Session)
        finally:
            await engine.dispose()

    asyncio.run(go())


async def _seed(Session, *, balance: int = 100_000):
    async with Session() as s:
        p = Panel(key="p1", host="p1.invalid", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
        s.add(p)
        await s.flush()
        r = Reseller(panel_id=p.id, admin_uuid="A1", name="Ali", storefront_enabled=True)
        s.add(r)
        await s.flush()
        bot = StorefrontBot(
            reseller_id=r.id, panel_id=p.id, bot_token_enc=crypto.encrypt("t"),
            bot_username="shop", enabled=True, status="active",
        )
        s.add(bot)
        await s.flush()
        cust = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=777, name="C")
        s.add(cust)
        await s.flush()
        plan = StorefrontPlan(
            storefront_bot_id=bot.id, title="", gb=10, days=30, price_toman=50_000,
            enabled=True, sort_order=0,
        )
        s.add(plan)
        await s.flush()
        await storefront_wallet.manual_adjust(s, cust, balance, note="seed")
        await s.commit()
        return bot.id, cust.id, plan.id


async def _balance(Session, customer_id: int) -> int:
    async with Session() as s:
        c = await s.get(StorefrontCustomer, customer_id)
        return int(storefront_wallet.balance(c))


async def _order(Session, customer_id: int) -> StorefrontOrder:
    async with Session() as s:
        rows = (await s.execute(
            StorefrontOrder.__table__.select().where(
                StorefrontOrder.customer_id == customer_id)
        )).all()
        assert len(rows) == 1, f"expected one order, got {len(rows)}"
        return await s.get(StorefrontOrder, rows[0].id)


async def _txn_kinds(Session) -> list[str]:
    async with Session() as s:
        rows = (await s.execute(StorefrontWalletTxn.__table__.select())).all()
        return [r.kind for r in rows]


async def _park_order(Session, order_id: int, status: str, *, lease_minutes: int = 5):
    """Leave an order exactly as another PROCESS would observe it mid-flight."""
    async with Session() as s:
        o = await s.get(StorefrontOrder, order_id)
        o.status = status
        o.lease_expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=lease_minutes)
        await s.commit()


async def _buy(Session, monkeypatch, sf_id, cid, pid):
    async def ok_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001
        uid = user_uuid or "u1"
        return SimpleNamespace(
            created=[SimpleNamespace(name=base_name, uuid=uid,
                                     sub_link=f"https://h/p/{uid}/#x")],
            error=None, capacity_blocked=False, limit_hit=False)

    monkeypatch.setattr(usercreate, "create_for_reseller", ok_create)
    return await storefront_provision.purchase(
        Session, sf_id=sf_id, customer_id=cid, plan_id=pid, label="x")


def test_delete_is_refused_on_an_order_that_is_mid_purchase(tmp_path, monkeypatch):
    """The delete arrives from ANOTHER PROCESS while a purchase is provisioning.

    That cross-process case is the one that matters: `_customer_lock` is an in-process
    `asyncio.Lock`, so it already serializes a delete against a purchase inside the same container —
    but the portal/admin delete runs in the backend while a customer's purchase runs in the bot, and
    there the lock arbitrates nothing. Only the status + lease guard covers it, so that is what is
    exercised here: an order left exactly as the other process would see it (`pending`, live lease).
    """
    async def body(Session):
        sf_id, cid, pid = await _seed(Session)
        buy = await _buy(Session, monkeypatch, sf_id, cid, pid)
        assert buy.ok
        await _park_order(Session, buy.order_id, "pending")

        res = await storefront_subscription.delete_subscription(
            Session, order_id=buy.order_id, expected_sf_id=sf_id)

        assert res.ok is False, "delete succeeded on an order that is mid-purchase"
        assert res.reason == "busy"
        async with Session() as s:
            assert (await s.get(StorefrontOrder, buy.order_id)).status == "pending"
        # And nothing touched the money.
        assert await _balance(Session, cid) == 50_000
        assert await _txn_kinds(Session) == ["manual_credit", "purchase"]

    _run(body, tmp_path, "buy_delete.db")


def test_delete_is_refused_on_an_order_that_is_mid_renewal(tmp_path, monkeypatch):
    """Same guard for `renewing`, where a delete would destroy the config the renewal just paid for."""
    async def body(Session):
        sf_id, cid, pid = await _seed(Session, balance=200_000)
        buy = await _buy(Session, monkeypatch, sf_id, cid, pid)
        await _park_order(Session, buy.order_id, "renewing")

        res = await storefront_subscription.delete_subscription(
            Session, order_id=buy.order_id, expected_sf_id=sf_id)
        assert res.ok is False and res.reason == "busy"

    _run(body, tmp_path, "renew_delete_guard.db")


def test_the_in_process_lock_also_serializes_a_concurrent_delete(tmp_path, monkeypatch):
    """Within one process the lock is the first line of defence: a delete issued WHILE a purchase
    runs must wait for it rather than interleave, so it can never observe a half-finished order."""
    async def body(Session):
        sf_id, cid, pid = await _seed(Session)
        order_seen: dict = {}

        async def slow_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001
            await asyncio.sleep(0.05)          # hold the lock across a realistic panel call
            uid = user_uuid or "u1"
            return SimpleNamespace(
                created=[SimpleNamespace(name=base_name, uuid=uid,
                                         sub_link=f"https://h/p/{uid}/#x")],
                error=None, capacity_blocked=False, limit_hit=False)

        from app.services.panel_client import admin_api

        async def fake_delete_user(self, panel, uuid, *, api_key=None):  # noqa: ANN001
            return None

        monkeypatch.setattr(usercreate, "create_for_reseller", slow_create)
        monkeypatch.setattr(admin_api.AdminApiClient, "delete_user", fake_delete_user)

        buy_task = asyncio.create_task(storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=cid, plan_id=pid, label="x"))
        await asyncio.sleep(0.01)              # let the purchase take the lock

        async def delete_when_known():
            # Wait for the order row to exist, then race the still-running purchase.
            for _ in range(200):
                async with Session() as s:
                    row = (await s.execute(
                        StorefrontOrder.__table__.select().where(
                            StorefrontOrder.customer_id == cid))).first()
                if row is not None:
                    order_seen["id"] = row.id
                    return await storefront_subscription.delete_subscription(
                        Session, order_id=row.id, expected_sf_id=sf_id)
                await asyncio.sleep(0.005)
            raise AssertionError("the order never appeared")

        buy, delete = await asyncio.gather(buy_task, delete_when_known())

        assert buy.ok, buy.reason
        # The delete waited for the lock, so by the time it ran the order was settled and it
        # succeeded cleanly — the customer is charged once and ends with no service.
        assert delete.ok, delete.reason
        assert await _balance(Session, cid) == 50_000
        assert (await _txn_kinds(Session)).count("purchase") == 1
        async with Session() as s:
            assert (await s.get(StorefrontOrder, order_seen["id"])).status == "deleted"

    _run(body, tmp_path, "lock_serializes.db")


def test_a_purchase_finalized_elsewhere_still_refunds(tmp_path, monkeypatch):
    """Defence in depth for the same money. Even if an order does leave `pending` mid-provision by
    some other route, the committed debit must come back — `purchase` used to assume the only other
    writer was the reaper (which refunds) and so refunded nothing."""
    async def body(Session):
        sf_id, cid, pid = await _seed(Session)

        async def fake_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001
            # Simulate a concurrent terminal write that does NOT refund (what a delete used to do).
            async with Session() as s:
                o = (await s.execute(
                    StorefrontOrder.__table__.select().where(
                        StorefrontOrder.customer_id == cid)
                )).first()
                row = await s.get(StorefrontOrder, o.id)
                row.status = "deleted"
                await s.commit()
            uid = user_uuid or "u1"
            return SimpleNamespace(
                created=[SimpleNamespace(name=base_name, uuid=uid,
                                         sub_link=f"https://h/p/{uid}/#x")],
                error=None, capacity_blocked=False, limit_hit=False)

        monkeypatch.setattr(usercreate, "create_for_reseller", fake_create)

        res = await storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=cid, plan_id=pid, label="x")
        assert res.ok is False and res.reason == "reaped"

        assert await _balance(Session, cid) == 100_000, (
            "customer paid for a service that was cancelled mid-provision and was not refunded"
        )
        assert "refund" in await _txn_kinds(Session)

    _run(body, tmp_path, "buy_reaped.db")


def test_the_refund_is_not_duplicated_when_the_reaper_already_refunded(tmp_path, monkeypatch):
    """`order_has_refund` keeps it exactly-once, so the new refund cannot double-credit."""
    async def body(Session):
        sf_id, cid, pid = await _seed(Session)

        async def fake_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001
            async with Session() as s:
                o = (await s.execute(
                    StorefrontOrder.__table__.select().where(
                        StorefrontOrder.customer_id == cid)
                )).first()
                row = await s.get(StorefrontOrder, o.id)
                row.status = "failed"
                # The reaper's own refund, exactly as it would have issued it.
                await storefront_wallet.refund(
                    s, cid, 50_000, order_id=row.id, note="reaper")
                await s.commit()
            uid = user_uuid or "u1"
            return SimpleNamespace(
                created=[SimpleNamespace(name=base_name, uuid=uid,
                                         sub_link=f"https://h/p/{uid}/#x")],
                error=None, capacity_blocked=False, limit_hit=False)

        monkeypatch.setattr(usercreate, "create_for_reseller", fake_create)
        await storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=cid, plan_id=pid, label="x")

        assert await _balance(Session, cid) == 100_000, "refunded twice"
        assert (await _txn_kinds(Session)).count("refund") == 1

    _run(body, tmp_path, "buy_norefund_dup.db")


def test_a_renewal_does_not_resurrect_an_order_terminalised_mid_flight(tmp_path, monkeypatch):
    """The renewal's success write was unconditional, so a delete that slipped through flipped the
    order back to `provisioned` with no panel config behind it. It must now stand down and return
    the money instead."""
    from app.services.panel_client import admin_api

    async def body(Session):
        sf_id, cid, pid = await _seed(Session, balance=200_000)

        async def ok_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001
            uid = user_uuid or "u1"
            return SimpleNamespace(
                created=[SimpleNamespace(name=base_name, uuid=uid,
                                         sub_link=f"https://h/p/{uid}/#x")],
                error=None, capacity_blocked=False, limit_hit=False)

        monkeypatch.setattr(usercreate, "create_for_reseller", ok_create)
        buy = await storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=cid, plan_id=pid, label="x")
        order_id = buy.order_id

        async def fake_prepare(self, panel, uuid, *, gb, days, api_key=None):  # noqa: ANN001
            return admin_api.RenewUserTarget(
                usage_limit_gb=float(gb), package_days=int(days), prior_start_date=None)

        async def fake_apply(self, panel, uuid, target, *, api_key=None):  # noqa: ANN001
            # Force the exact interleaving the guard exists for: bypass delete_subscription and
            # terminal-ise the row directly, as a stale code path or an admin tool might.
            async with Session() as s:
                o = await s.get(StorefrontOrder, order_id)
                o.status = "deleted"
                o.lease_expires_at = None
                await s.commit()

        monkeypatch.setattr(admin_api.AdminApiClient, "prepare_renew_user", fake_prepare)
        monkeypatch.setattr(admin_api.AdminApiClient, "apply_renew_user_target", fake_apply)

        res = await storefront_subscription.renew(Session, order_id=order_id)

        assert res.ok is False and res.reason == "cancelled"
        async with Session() as s:
            o = await s.get(StorefrontOrder, order_id)
            assert o.status == "deleted", "a deleted order was resurrected as provisioned"
        # The renewal charge came back; the original purchase stands.
        assert await _balance(Session, cid) == 150_000
        assert "renew_reversal" in await _txn_kinds(Session)

    _run(body, tmp_path, "renew_resurrect.db")


def test_deleting_a_settled_order_still_works(tmp_path, monkeypatch):
    """The guard must not break ordinary deletion — the overwhelmingly common case."""
    from app.services.panel_client import admin_api

    seen: dict = {}

    async def fake_delete_user(self, panel, uuid, *, api_key=None):  # noqa: ANN001
        seen["uuid"] = uuid
        seen["api_key"] = api_key

    async def body(Session):
        sf_id, cid, pid = await _seed(Session)

        async def ok_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001
            uid = user_uuid or "u1"
            return SimpleNamespace(
                created=[SimpleNamespace(name=base_name, uuid=uid,
                                         sub_link=f"https://h/p/{uid}/#x")],
                error=None, capacity_blocked=False, limit_hit=False)

        monkeypatch.setattr(usercreate, "create_for_reseller", ok_create)
        monkeypatch.setattr(admin_api.AdminApiClient, "delete_user", fake_delete_user)
        buy = await storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=cid, plan_id=pid, label="x")

        res = await storefront_subscription.delete_subscription(
            Session, order_id=buy.order_id, expected_sf_id=sf_id)
        assert res.ok, res.reason
        assert seen["api_key"] == "A1"        # still tenant-scoped (v1.92.2)
        async with Session() as s:
            assert (await s.get(StorefrontOrder, buy.order_id)).status == "deleted"

    _run(body, tmp_path, "delete_ok.db")


def test_a_stale_lease_does_not_block_deletion_forever(tmp_path, monkeypatch):
    """A crashed purchase leaves a lease behind. Once it EXPIRES the order must be deletable again,
    or a customer could be locked out of their own service permanently."""
    from app.services.panel_client import admin_api

    async def fake_delete_user(self, panel, uuid, *, api_key=None):  # noqa: ANN001
        return None

    async def body(Session):
        sf_id, cid, pid = await _seed(Session)

        async def ok_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001
            uid = user_uuid or "u1"
            return SimpleNamespace(
                created=[SimpleNamespace(name=base_name, uuid=uid,
                                         sub_link=f"https://h/p/{uid}/#x")],
                error=None, capacity_blocked=False, limit_hit=False)

        monkeypatch.setattr(usercreate, "create_for_reseller", ok_create)
        monkeypatch.setattr(admin_api.AdminApiClient, "delete_user", fake_delete_user)
        buy = await storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=cid, plan_id=pid, label="x")

        async with Session() as s:
            o = await s.get(StorefrontOrder, buy.order_id)
            o.lease_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
            await s.commit()

        res = await storefront_subscription.delete_subscription(
            Session, order_id=buy.order_id, expected_sf_id=sf_id)
        assert res.ok, f"an expired lease still blocked deletion: {res.reason}"

    _run(body, tmp_path, "stale_lease.db")


def test_a_foreign_shop_still_gets_not_found_not_busy(tmp_path, monkeypatch):
    """Tenant isolation outranks the new refusal: a cross-tenant caller must not be able to learn
    that an order exists and is mid-purchase."""
    async def body(Session):
        sf_id, cid, pid = await _seed(Session)

        async def ok_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001
            uid = user_uuid or "u1"
            return SimpleNamespace(
                created=[SimpleNamespace(name=base_name, uuid=uid,
                                         sub_link=f"https://h/p/{uid}/#x")],
                error=None, capacity_blocked=False, limit_hit=False)

        monkeypatch.setattr(usercreate, "create_for_reseller", ok_create)
        buy = await storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=cid, plan_id=pid, label="x")

        async with Session() as s:
            o = await s.get(StorefrontOrder, buy.order_id)
            o.status = "pending"
            o.lease_expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)
            await s.commit()

        res = await storefront_subscription.delete_subscription(
            Session, order_id=buy.order_id, expected_sf_id=sf_id + 999)
        assert res.reason == "not_found", (
            "a foreign shop learned this order is mid-purchase"
        )

    _run(body, tmp_path, "foreign.db")


# ── PG barrier: the row lock must actually arbitrate on real Postgres ─────────────────────────
# SQLite makes `FOR UPDATE` a no-op, so the serialization asserted below can only be proven here.
# Runs in CI's `backend-postgres` job (`pytest -m pg_contract`).
import pytest  # noqa: E402

from tests.pg_barrier import make_engine, requires_pg  # noqa: E402


async def _pg_seed_shop(Session, tag: str):
    """Unique per test and self-cleaning — these share one CI database."""
    await _pg_purge_shop(Session, tag)
    async with Session() as s:
        p = Panel(key=f"sfrace-{tag}", host=f"{tag}.sfrace.invalid",
                  proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
        s.add(p)
        await s.flush()
        r = Reseller(panel_id=p.id, admin_uuid=f"SFRACE-{tag}", name="Ali",
                     storefront_enabled=True)
        s.add(r)
        await s.flush()
        bot = StorefrontBot(reseller_id=r.id, panel_id=p.id, bot_token_enc=crypto.encrypt("t"),
                            bot_username=f"shop{tag}", enabled=True, status="active")
        s.add(bot)
        await s.flush()
        cust = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=987_654, name="C")
        s.add(cust)
        await s.flush()
        plan = StorefrontPlan(storefront_bot_id=bot.id, title="", gb=10, days=30,
                              price_toman=50_000, enabled=True, sort_order=0)
        s.add(plan)
        await s.flush()
        await storefront_wallet.manual_adjust(s, cust, 100_000, note="seed")
        await s.commit()
        return bot.id, cust.id, plan.id


async def _pg_purge_shop(Session, tag: str):
    from sqlalchemy import select as _select

    async with Session() as s:
        pid = (await s.execute(
            _select(Panel.id).where(Panel.key == f"sfrace-{tag}"))).scalar_one_or_none()
        if pid is None:
            return
        bots = (await s.execute(
            _select(StorefrontBot.id).where(StorefrontBot.panel_id == pid))).scalars().all()
        custs = (await s.execute(_select(StorefrontCustomer.id).where(
            StorefrontCustomer.storefront_bot_id.in_(bots or [-1])))).scalars().all()
        from app.models import StorefrontOperation

        await s.execute(StorefrontWalletTxn.__table__.delete().where(
            StorefrontWalletTxn.customer_id.in_(custs or [-1])))
        await s.execute(StorefrontOperation.__table__.delete().where(
            StorefrontOperation.customer_id.in_(custs or [-1])))
        await s.execute(StorefrontOrder.__table__.delete().where(
            StorefrontOrder.customer_id.in_(custs or [-1])))
        await s.execute(StorefrontCustomer.__table__.delete().where(
            StorefrontCustomer.id.in_(custs or [-1])))
        await s.execute(StorefrontPlan.__table__.delete().where(
            StorefrontPlan.storefront_bot_id.in_(bots or [-1])))
        await s.execute(StorefrontBot.__table__.delete().where(StorefrontBot.id.in_(bots or [-1])))
        await s.execute(Reseller.__table__.delete().where(Reseller.panel_id == pid))
        await s.execute(Panel.__table__.delete().where(Panel.id == pid))
        await s.commit()


@pytest.mark.pg_contract
@requires_pg
def test_pg_delete_refuses_an_order_another_process_is_provisioning(monkeypatch):
    """The cross-process case on real Postgres: the delete cannot see the other process's
    in-memory lock, so only the status + lease guard stands between it and the customer's money."""
    async def run():
        engine, Session = make_engine()
        try:
            sf_id, cid, pid = await _pg_seed_shop(Session, "busy")

            async def ok_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001
                uid = user_uuid or "u1"
                return SimpleNamespace(
                    created=[SimpleNamespace(name=base_name, uuid=uid,
                                             sub_link=f"https://h/p/{uid}/#x")],
                    error=None, capacity_blocked=False, limit_hit=False)

            monkeypatch.setattr(usercreate, "create_for_reseller", ok_create)
            buy = await storefront_provision.purchase(
                Session, sf_id=sf_id, customer_id=cid, plan_id=pid, label="x")
            assert buy.ok, buy.reason
            await _park_order(Session, buy.order_id, "pending")

            res = await storefront_subscription.delete_subscription(
                Session, order_id=buy.order_id, expected_sf_id=sf_id)
            assert res.ok is False and res.reason == "busy"

            async with Session() as s:
                assert (await s.get(StorefrontOrder, buy.order_id)).status == "pending"
            await _pg_purge_shop(Session, "busy")
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.pg_contract
@requires_pg
def test_pg_two_concurrent_deletes_settle_the_order_once(monkeypatch):
    """A double-tap must not run the panel delete twice or produce contradictory results."""
    async def run():
        from app.services.panel_client import admin_api

        calls = {"n": 0}

        async def fake_delete_user(self, panel, uuid, *, api_key=None):  # noqa: ANN001
            calls["n"] += 1

        engine, Session = make_engine()
        try:
            sf_id, cid, pid = await _pg_seed_shop(Session, "dbldel")

            async def ok_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001
                uid = user_uuid or "u1"
                return SimpleNamespace(
                    created=[SimpleNamespace(name=base_name, uuid=uid,
                                             sub_link=f"https://h/p/{uid}/#x")],
                    error=None, capacity_blocked=False, limit_hit=False)

            monkeypatch.setattr(usercreate, "create_for_reseller", ok_create)
            monkeypatch.setattr(admin_api.AdminApiClient, "delete_user", fake_delete_user)
            buy = await storefront_provision.purchase(
                Session, sf_id=sf_id, customer_id=cid, plan_id=pid, label="x")

            a, b = await asyncio.gather(
                storefront_subscription.delete_subscription(
                    Session, order_id=buy.order_id, expected_sf_id=sf_id),
                storefront_subscription.delete_subscription(
                    Session, order_id=buy.order_id, expected_sf_id=sf_id),
                return_exceptions=True)
            for r in (a, b):
                assert not isinstance(r, BaseException), (a, b)
            assert sum(1 for r in (a, b) if r.ok) == 1, f"both deletes claimed success: {a}, {b}"

            async with Session() as s:
                assert (await s.get(StorefrontOrder, buy.order_id)).status == "deleted"
            await _pg_purge_shop(Session, "dbldel")
        finally:
            await engine.dispose()

    asyncio.run(run())
