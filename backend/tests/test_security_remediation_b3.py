"""Security remediation — Batch 3 (storefront provisioning robustness).

F5  the pending-order reaper locks each order (SKIP LOCKED) and never reaps one a live provision holds.
F5  the reaper recovers an order stuck in "renewing" (renew() crashed) back to provisioned.
F15 the per-customer in-process lock registry evicts idle locks (can't grow with churn).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import gc
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/secremb3.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy import update  # noqa: E402

from app.models import (  # noqa: E402
    StorefrontCustomer,
    StorefrontOrder,
    StorefrontWalletTxn,
)
from app.services import storefront_provision  # noqa: E402
from tests.pg_barrier import make_engine, requires_pg  # noqa: E402
from tests.test_security_remediation_b2 import _run, _shop  # noqa: E402


def _order(cust, **kw):  # noqa: ANN001, ANN003
    base = dict(customer_id=cust.id, panel_id=1, panel_user_uuid="u-1", is_trial=False,
                gb=5, days=30, price_toman=10_000, label="svc")
    base.update(kw)
    return StorefrontOrder(**base)


# ───────────── F5: reaper recovers a stuck "renewing" order ─────────────
def test_f5_reaper_recovers_stuck_renewing(tmp_path):
    async def body(s, _S):
        _bot, cust = await _shop(s, "a")
        o = _order(cust, status="renewing")
        s.add(o)
        await s.flush()
        oid = o.id
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        await s.execute(update(StorefrontOrder).where(StorefrontOrder.id == oid)
                        .values(updated_at=old))   # make it stale (bypass onupdate)
        await s.commit()
        await storefront_provision.reap_pending_orders(
            s, older_than=dt.datetime.now(dt.timezone.utc))
        o2 = await s.get(StorefrontOrder, oid)
        assert o2.status == "provisioned"  # recovered, not stuck
    _run(body, tmp_path, "b3renew.db")


# ───────────── F5: a lost pending order is refunded exactly once ─────────────
def test_f5_reaper_refunds_lost_order_once(tmp_path, monkeypatch):
    async def _no_user(self, *a, **k):  # noqa: ANN001, ANN002, ANN003 — panel has no such user
        return None
    monkeypatch.setattr(
        "app.services.panel_client.admin_api.AdminApiClient.get_user", _no_user)

    async def body(s, _S):
        _bot, cust = await _shop(s, "a", balance=0)
        o = _order(cust, status="pending")
        s.add(o)
        await s.flush()
        oid = o.id
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        await s.execute(update(StorefrontOrder).where(StorefrontOrder.id == oid)
                        .values(created_at=old))
        await s.commit()
        await storefront_provision.reap_pending_orders(
            s, older_than=dt.datetime.now(dt.timezone.utc))
        # running it AGAIN must not double-refund (order_has_refund + the partial-unique index)
        await storefront_provision.reap_pending_orders(
            s, older_than=dt.datetime.now(dt.timezone.utc))
        o2 = await s.get(StorefrontOrder, oid)
        assert o2.status == "failed"
        from sqlalchemy import func, select
        n = (await s.execute(select(func.count()).select_from(StorefrontWalletTxn).where(
            StorefrontWalletTxn.order_id == oid, StorefrontWalletTxn.kind == "refund"))).scalar_one()
        assert n == 1  # exactly one refund
        c = await s.get(StorefrontCustomer, cust.id)
        assert storefront_wallet_balance(c) == 10_000
    _run(body, tmp_path, "b3refund.db")


def storefront_wallet_balance(c):  # noqa: ANN001, ANN201
    from app.services import storefront_wallet
    return storefront_wallet.balance(c)


# ───────────── F5 (PG): the reaper skips an order a live provision holds ─────────────
@pytest.mark.pg_contract
@requires_pg
def test_f5_reaper_skips_locked_order():
    async def run():
        engine, factory = make_engine()
        try:
            async with factory() as s:
                bot, cust = await _shop(s, "z", balance=0)
                o = _order(cust, status="pending")
                s.add(o)
                await s.flush()
                oid, cid = o.id, cust.id
                old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
                await s.execute(update(StorefrontOrder).where(StorefrontOrder.id == oid)
                                .values(created_at=old))
                await s.commit()

            held = asyncio.Event()
            release = asyncio.Event()

            async def _holder():   # simulate a live provision holding the order row lock
                async with factory() as s:
                    locked = await storefront_provision._lock_order_skip(s, oid)
                    assert locked is not None
                    held.set()
                    await release.wait()
                    await s.rollback()

            async def _reap():
                await held.wait()
                async with factory() as s:
                    await storefront_provision.reap_pending_orders(
                        s, older_than=dt.datetime.now(dt.timezone.utc))
                release.set()

            await asyncio.gather(_holder(), _reap())

            async with factory() as s:
                o2 = await s.get(StorefrontOrder, oid)
                from sqlalchemy import func, select
                refunds = (await s.execute(select(func.count()).select_from(StorefrontWalletTxn)
                           .where(StorefrontWalletTxn.order_id == oid,
                                  StorefrontWalletTxn.kind == "refund"))).scalar_one()
                assert o2.status == "pending"   # left for the next tick, not reaped
                assert refunds == 0             # never refunded while a live provision held it
                # cleanup
                await s.execute(StorefrontWalletTxn.__table__.delete().where(
                    StorefrontWalletTxn.order_id == oid))
                await s.delete(o2)
                c = await s.get(StorefrontCustomer, cid)
                b = await s.get(type(bot), c.storefront_bot_id)
                from app.models import Panel, Reseller
                r_id, p_id = b.reseller_id, b.panel_id
                await s.delete(c)
                await s.delete(b)
                await s.delete(await s.get(Reseller, r_id))
                await s.delete(await s.get(Panel, p_id))
                await s.commit()
        finally:
            await engine.dispose()
    asyncio.run(run())


# ───────────── F15: the per-customer lock registry evicts idle locks ─────────────
def test_f15_customer_lock_registry_evicts():
    async def go():
        for i in range(1000):
            async with storefront_provision._customer_lock(1, i):
                pass
        gc.collect()
        # every lock was released + unreferenced → GC'd out of the WeakValueDictionary
        assert len(storefront_provision._customer_locks) < 10
    asyncio.run(go())
