"""Security remediation — Round 2, Batch B (storefront money durability).

F4  purchase/renewal are idempotent by a durable `op_id` (a replay returns the cached result, never a
    second debit).
F11 a crashed renewal persists an absolute panel target before debit; the reconciler verifies or
    idempotently reapplies it and only compensates when the panel user is definitively absent.
F5  the reaper never refunds an order whose provisioning LEASE is still active.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/secremr2b.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import (  # noqa: E402
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOperation,
    StorefrontOrder,
    StorefrontPlan,
    StorefrontWalletTxn,
)
from app.services import (  # noqa: E402
    storefront,
    storefront_ops,
    storefront_provision,
    storefront_subscription,
    usercreate,
)
from app.services.panel_client import admin_api  # noqa: E402
from tests.pg_barrier import make_engine, requires_pg  # noqa: E402


def _engine_session(tmp_path, name):  # noqa: ANN001, ANN202
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _create_all(engine):  # noqa: ANN001
    from app.core.db import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed(s, *, balance=100_000):  # noqa: ANN001
    p = Panel(key="pB", host="pB.invalid", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
    s.add(p)
    await s.flush()
    r = Reseller(panel_id=p.id, admin_uuid="AB", name="Ali", storefront_enabled=True)
    s.add(r)
    await s.flush()
    bot = StorefrontBot(reseller_id=r.id, panel_id=p.id, bot_token_enc=crypto.encrypt("1:a") or "",
                        bot_telegram_id=9911, enabled=True)
    s.add(bot)
    await s.flush()
    cust = await storefront.get_or_create_customer(
        s, bot.id, SimpleNamespace(id=555, first_name="Cust", username="c"))
    cust.wallet_balance_toman = balance
    plan = StorefrontPlan(storefront_bot_id=bot.id, gb=10, days=30, price_toman=30_000, enabled=True)
    s.add(plan)
    await s.commit()
    return r, bot, cust, plan


def _fake_create(calls):  # noqa: ANN001, ANN202
    async def create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001, ANN002
        calls["create"] = calls.get("create", 0) + 1
        uid = user_uuid or f"u{calls['create']}"
        return SimpleNamespace(
            created=[SimpleNamespace(name=base_name, uuid=uid, sub_link=f"https://h/p/{uid}/#x")],
            error=None, capacity_blocked=False, limit_hit=False)
    return create


def _fake_renew(calls):  # noqa: ANN001, ANN202
    async def prepare(self, panel, uuid, *, gb, days, api_key=None):  # noqa: ANN001, ANN002
        return admin_api.RenewUserTarget(float(gb), int(days), "2026-06-01")

    async def apply(self, panel, uuid, target, *, api_key=None):  # noqa: ANN001, ANN002
        calls["renew"] = calls.get("renew", 0) + 1
    return prepare, apply


async def _purchase_txn_count(Session, cust_id):  # noqa: ANN001
    async with Session() as s:
        return (await s.execute(
            select(func.count()).select_from(StorefrontWalletTxn).where(
                StorefrontWalletTxn.customer_id == cust_id,
                StorefrontWalletTxn.kind == "purchase"))).scalar_one()


# ───────────────────────── F4: renewal idempotency ─────────────────────────
def test_f4_renewal_idempotent_replay(tmp_path, monkeypatch):
    calls: dict[str, int] = {}
    prepare, apply = _fake_renew(calls)
    monkeypatch.setattr(admin_api.AdminApiClient, "prepare_renew_user", prepare)
    monkeypatch.setattr(admin_api.AdminApiClient, "apply_renew_user_target", apply)

    async def go():
        engine, Session = _engine_session(tmp_path, "r2b_ren.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s)
            order = StorefrontOrder(customer_id=cust.id, plan_id=plan.id, panel_id=bot.panel_id,
                                    label="svc", gb=10, days=30, price_toman=30_000,
                                    status="provisioned", panel_user_uuid="uuid-1",
                                    sub_link="https://h/p/uuid-1/#x")
            s.add(order)
            await s.commit()
            oid, cid = order.id, cust.id

        r1 = await storefront_subscription.renew(Session, order_id=oid, op_id="OP-REN")
        assert r1.ok, r1
        assert await _purchase_txn_count(Session, cid) == 1
        assert calls.get("renew") == 1
        async with Session() as s:
            assert int((await s.get(StorefrontCustomer, cid)).wallet_balance_toman) == 70_000

        # REPLAY with the same op_id → cached success, NO second debit / panel call
        r2 = await storefront_subscription.renew(Session, order_id=oid, op_id="OP-REN")
        assert r2.ok, r2
        assert await _purchase_txn_count(Session, cid) == 1   # still one debit
        assert calls.get("renew") == 1                        # panel not called again
        async with Session() as s:
            assert int((await s.get(StorefrontCustomer, cid)).wallet_balance_toman) == 70_000
        await engine.dispose()

    asyncio.run(go())


# ───────────────────────── F4: purchase idempotency ─────────────────────────
def test_f4_purchase_idempotent_replay(tmp_path, monkeypatch):
    calls: dict[str, int] = {}
    monkeypatch.setattr(usercreate, "create_for_reseller", _fake_create(calls))

    async def go():
        engine, Session = _engine_session(tmp_path, "r2b_buy.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s)
            sf_id, cid, plan_id = bot.id, cust.id, plan.id

        r1 = await storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=cid, plan_id=plan_id, label="svc", op_id="OP-BUY")
        assert r1.ok and r1.sub_link, r1
        assert calls.get("create") == 1
        assert await _purchase_txn_count(Session, cid) == 1

        # REPLAY → cached same order/sub_link, NO second order or debit
        r2 = await storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=cid, plan_id=plan_id, label="svc", op_id="OP-BUY")
        assert r2.ok and r2.sub_link == r1.sub_link and r2.order_id == r1.order_id, r2
        assert calls.get("create") == 1
        assert await _purchase_txn_count(Session, cid) == 1
        async with Session() as s:
            n_orders = (await s.execute(
                select(func.count()).select_from(StorefrontOrder).where(
                    StorefrontOrder.customer_id == cid))).scalar_one()
            assert n_orders == 1
            assert int((await s.get(StorefrontCustomer, cid)).wallet_balance_toman) == 70_000
        await engine.dispose()

    asyncio.run(go())


def test_f4_purchase_replay_is_bound_to_customer_and_tenant(tmp_path, monkeypatch):
    calls: dict[str, int] = {}
    monkeypatch.setattr(usercreate, "create_for_reseller", _fake_create(calls))

    async def go():
        engine, Session = _engine_session(tmp_path, "r2b_bind.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, owner, plan = await _seed(s)
            other = await storefront.get_or_create_customer(
                s, bot.id, SimpleNamespace(id=777, first_name="Other", username="other"))
            other.wallet_balance_toman = 100_000
            await storefront_ops.reserve(
                s, "OP-BOUND", op_type="purchase", storefront_bot_id=bot.id,
                customer_id=owner.id, plan_id=plan.id, price_toman=plan.price_toman,
                gb=plan.gb, days=plan.days)
            await s.commit()
            ids = bot.id, owner.id, other.id, plan.id

        sf_id, owner_id, other_id, plan_id = ids
        preclaim_attack = await storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=other_id, plan_id=plan_id,
            label="attacker", op_id="OP-BOUND")
        assert not preclaim_attack.ok and preclaim_attack.reason == "invalid_operation"

        original = await storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=owner_id, plan_id=plan_id,
            label="owner", op_id="OP-BOUND")
        assert original.ok and original.sub_link

        replay = await storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=other_id, plan_id=0,
            label="", op_id="OP-BOUND")
        assert not replay.ok and replay.reason == "invalid_operation"
        assert replay.order_id is None and replay.sub_link is None
        async with Session() as s:
            assert int((await s.get(StorefrontCustomer, other_id)).wallet_balance_toman) == 100_000
        await engine.dispose()

    asyncio.run(go())


def test_f5_lease_clamps_legacy_short_setting_above_panel_sequence(tmp_path, monkeypatch):
    async def short_setting(*args, **kwargs):  # noqa: ANN002, ANN003
        return 30

    monkeypatch.setattr(storefront_ops.settings_service, "get", short_setting)

    async def go():
        engine, Session = _engine_session(tmp_path, "r2b_lease_bound.db")
        await _create_all(engine)
        async with Session() as s:
            assert await storefront_ops.lease_seconds(s) == 300
        await engine.dispose()

    asyncio.run(go())


# ───────────────────────── F11: reconciler reverses a crashed renewal once ─────────────────────────
def test_f11_reconciler_reverses_crashed_renewal_once(tmp_path):
    async def go():
        engine, Session = _engine_session(tmp_path, "r2b_crash.db")
        await _create_all(engine)
        past = storefront_ops.now() - dt.timedelta(minutes=5)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=70_000)   # already debited 30k
            order = StorefrontOrder(customer_id=cust.id, plan_id=plan.id, panel_id=bot.panel_id,
                                    label="svc", gb=10, days=30, price_toman=30_000,
                                    status="renewing", panel_user_uuid="uuid-1",
                                    sub_link="https://h/p/uuid-1/#x", lease_expires_at=past)
            s.add(order)
            await s.flush()
            op = StorefrontOperation(op_id="OP-CRASH", op_type="renewal", storefront_bot_id=bot.id,
                                     customer_id=cust.id, order_id=order.id, plan_id=plan.id,
                                     status="in_progress", prior_status="provisioned",
                                     price_toman=30_000)
            s.add(op)
            await s.flush()
            debit = StorefrontWalletTxn(customer_id=cust.id, storefront_bot_id=bot.id, kind="purchase",
                                        amount_toman=-30_000, status="done", order_id=order.id,
                                        operation_id=op.id)
            s.add(debit)
            await s.commit()
            oid, cid, op_pk = order.id, cust.id, op.id

        future = storefront_ops.now() + dt.timedelta(hours=1)   # force the age gate
        async with Session() as s:
            res = await storefront_provision.reap_pending_orders(s, older_than=future)
        assert res["renewing_recovered"] == 1 and res["reversed"] == 1, res
        async with Session() as s:
            o = await s.get(StorefrontOrder, oid)
            assert o.status == "provisioned" and o.lease_expires_at is None
            assert (await s.get(StorefrontOperation, op_pk)).status == "reversed"
            assert int((await s.get(StorefrontCustomer, cid)).wallet_balance_toman) == 100_000  # made whole
            n_rev = (await s.execute(select(func.count()).select_from(StorefrontWalletTxn).where(
                StorefrontWalletTxn.operation_id == op_pk,
                StorefrontWalletTxn.kind == "renew_reversal"))).scalar_one()
            assert n_rev == 1

        # run the reconciler AGAIN → no second reversal (order no longer renewing; op terminal)
        async with Session() as s:
            res2 = await storefront_provision.reap_pending_orders(s, older_than=future)
        assert res2["reversed"] == 0
        async with Session() as s:
            n_rev = (await s.execute(select(func.count()).select_from(StorefrontWalletTxn).where(
                StorefrontWalletTxn.operation_id == op_pk,
                StorefrontWalletTxn.kind == "renew_reversal"))).scalar_one()
            assert n_rev == 1
        await engine.dispose()

    asyncio.run(go())


@pytest.mark.parametrize("already_applied", [True, False])
def test_f11_reconciler_finalizes_panel_success_without_refund(
    tmp_path, monkeypatch, already_applied,
):
    applied: list[admin_api.RenewUserTarget] = []

    async def panel_has_target(self, panel, uuid, *, api_key=None):  # noqa: ANN001, ANN002
        return {
            "usage_limit_GB": 18.0 if already_applied else 8.0,
            "package_days": 30 if already_applied else 10,
            "start_date": None if already_applied else "2026-06-01",
            "enable": True,
        }

    async def apply_target(self, panel, uuid, target, *, api_key=None):  # noqa: ANN001, ANN002
        applied.append(target)

    monkeypatch.setattr(admin_api.AdminApiClient, "get_user", panel_has_target)
    monkeypatch.setattr(admin_api.AdminApiClient, "apply_renew_user_target", apply_target)

    async def go():
        engine, Session = _engine_session(tmp_path, "r2b_panel_done.db")
        await _create_all(engine)
        past = storefront_ops.now() - dt.timedelta(minutes=5)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=70_000)
            order = StorefrontOrder(
                customer_id=cust.id, plan_id=plan.id, panel_id=bot.panel_id,
                label="svc", gb=10, days=30, price_toman=30_000, status="renewing",
                panel_user_uuid="uuid-1", sub_link="https://h/p/uuid-1/#x",
                lease_expires_at=past)
            s.add(order)
            await s.flush()
            op = StorefrontOperation(
                op_id="OP-PANEL-DONE", op_type="renewal", storefront_bot_id=bot.id,
                customer_id=cust.id, order_id=order.id, plan_id=plan.id, status="in_progress",
                prior_status="provisioned", price_toman=30_000, gb=10, days=30,
                target_usage_limit_gb=18.0, prior_panel_start_date="2026-06-01")
            s.add(op)
            await s.flush()
            s.add(StorefrontWalletTxn(
                customer_id=cust.id, storefront_bot_id=bot.id, kind="purchase",
                amount_toman=-30_000, status="done", order_id=order.id, operation_id=op.id))
            await s.commit()
            oid, cid, op_pk = order.id, cust.id, op.id

        async with Session() as s:
            result = await storefront_provision.reap_pending_orders(
                s, older_than=storefront_ops.now() + dt.timedelta(hours=1))
        assert result["renewing_completed"] == 1 and result["reversed"] == 0
        assert len(applied) == (0 if already_applied else 1)
        if applied:
            assert applied[0].usage_limit_gb == 18.0 and applied[0].package_days == 30
        async with Session() as s:
            assert (await s.get(StorefrontOperation, op_pk)).status == "done"
            assert (await s.get(StorefrontOrder, oid)).status == "provisioned"
            assert int((await s.get(StorefrontCustomer, cid)).wallet_balance_toman) == 70_000
            n_rev = (await s.execute(select(func.count()).select_from(StorefrontWalletTxn).where(
                StorefrontWalletTxn.operation_id == op_pk,
                StorefrontWalletTxn.kind == "renew_reversal"))).scalar_one()
            assert n_rev == 0
        await engine.dispose()

    asyncio.run(go())


# ───────────────────────── F5: an active lease shields an in-flight provision ─────────────────────────
def test_f5_active_lease_blocks_reaper(tmp_path, monkeypatch):
    async def fake_get_user(self, panel, uuid, *, api_key=None):  # noqa: ANN001, ANN002
        return None   # config not visible yet (as during a slow provision)
    monkeypatch.setattr(admin_api.AdminApiClient, "get_user", fake_get_user)

    async def go():
        engine, Session = _engine_session(tmp_path, "r2b_lease.db")
        await _create_all(engine)
        future = storefront_ops.now() + dt.timedelta(hours=1)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=70_000)
            # a pending order with an ACTIVE lease (a live provision holds it)
            order = StorefrontOrder(customer_id=cust.id, plan_id=plan.id, panel_id=bot.panel_id,
                                    label="svc", gb=10, days=30, price_toman=30_000, status="pending",
                                    panel_user_uuid="uuid-x", lease_expires_at=future)
            s.add(order)
            await s.commit()
            oid, cid = order.id, cust.id

        # reaper age gate forced open, but the lease is still valid → order must be untouched, no refund
        async with Session() as s:
            res = await storefront_provision.reap_pending_orders(s, older_than=future)
        assert res["refunded"] == 0 and res["checked"] == 0, res
        async with Session() as s:
            assert (await s.get(StorefrontOrder, oid)).status == "pending"
            assert int((await s.get(StorefrontCustomer, cid)).wallet_balance_toman) == 70_000

        # now expire the lease → the reaper refunds once (config absent on the panel) and fails it
        async with Session() as s:
            o = await s.get(StorefrontOrder, oid)
            o.lease_expires_at = storefront_ops.now() - dt.timedelta(minutes=1)
            await s.commit()
        async with Session() as s:
            res2 = await storefront_provision.reap_pending_orders(s, older_than=future)
        assert res2["refunded"] == 1, res2
        async with Session() as s:
            assert (await s.get(StorefrontOrder, oid)).status == "failed"
            assert int((await s.get(StorefrontCustomer, cid)).wallet_balance_toman) == 100_000  # refunded
        await engine.dispose()

    asyncio.run(go())


# ───────────────────────── PG barrier: concurrent renewals charge once ─────────────────────────
@pytest.mark.pg_contract
@requires_pg
def test_f4_concurrent_renewals_charge_once(monkeypatch):
    calls: dict[str, int] = {}
    prepare, apply = _fake_renew(calls)
    monkeypatch.setattr(admin_api.AdminApiClient, "prepare_renew_user", prepare)
    monkeypatch.setattr(admin_api.AdminApiClient, "apply_renew_user_target", apply)

    async def run():
        engine, Session = make_engine()
        try:
            async with Session() as s:
                _r, bot, cust, plan = await _seed(s)
                order = StorefrontOrder(customer_id=cust.id, plan_id=plan.id, panel_id=bot.panel_id,
                                        label="svc", gb=10, days=30, price_toman=30_000,
                                        status="provisioned", panel_user_uuid="uuid-1",
                                        sub_link="https://h/p/uuid-1/#x")
                s.add(order)
                await s.commit()
                oid, cid, pid, rid = order.id, cust.id, bot.panel_id, bot.reseller_id

            # SAME op_id twice concurrently → exactly one debit; the loser replays / bails, never charges.
            a, b = await asyncio.gather(
                storefront_subscription.renew(Session, order_id=oid, op_id="OP-RACE"),
                storefront_subscription.renew(Session, order_id=oid, op_id="OP-RACE"),
                return_exceptions=True)
            assert not isinstance(a, BaseException), a
            assert not isinstance(b, BaseException), b
            assert await _purchase_txn_count(Session, cid) == 1, (a, b)
            assert calls.get("renew") == 1
            async with Session() as s:
                assert int((await s.get(StorefrontCustomer, cid)).wallet_balance_toman) == 70_000
                # cleanup
                for model in (StorefrontWalletTxn, StorefrontOperation, StorefrontOrder,
                              StorefrontCustomer, StorefrontPlan, StorefrontBot):
                    await s.execute(model.__table__.delete())
                await s.execute(Reseller.__table__.delete().where(Reseller.id == rid))
                await s.execute(Panel.__table__.delete().where(Panel.id == pid))
                await s.commit()
        finally:
            await engine.dispose()
    asyncio.run(run())
