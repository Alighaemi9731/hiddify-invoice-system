"""Deferred one-shot auto-renew (Phase 2 of the storefront-renewal-billing program).

Covers the wallet hold primitive (place/release/settle, exactly-once), the arm/disarm service, and
the fire path — the money-correctness core: arming reserves exactly one plan price, firing settles
that reservation as the renewal (never a SECOND debit), it is one-shot (the arm clears on success),
and a dangling arm is cleaned up. A pg_contract barrier proves arm/fire/disarm can't double-spend
under concurrency.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/sfautorenew.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import (  # noqa: E402
    Panel,
    Reseller,
    StorefrontAuditEvent,  # noqa: E402
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOperation,
    StorefrontOrder,
    StorefrontPlan,
    StorefrontWalletTxn,
)
from app.services import (  # noqa: E402
    storefront,
    storefront_admin,
    storefront_autorenew,
    storefront_subscription,
    storefront_wallet,
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


async def _seed(s, *, balance=100_000, price=30_000):  # noqa: ANN001
    p = Panel(key="pAR", host="pAR.invalid", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
    s.add(p)
    await s.flush()
    r = Reseller(panel_id=p.id, admin_uuid="AR", name="Ali", storefront_enabled=True)
    s.add(r)
    await s.flush()
    bot = StorefrontBot(reseller_id=r.id, panel_id=p.id, bot_token_enc=crypto.encrypt("1:a") or "",
                        bot_telegram_id=9931, enabled=True)
    s.add(bot)
    await s.flush()
    cust = await storefront.get_or_create_customer(
        s, bot.id, SimpleNamespace(id=555, first_name="Cust", username="c"))
    cust.wallet_balance_toman = balance
    plan = StorefrontPlan(storefront_bot_id=bot.id, gb=10, days=30, price_toman=price, enabled=True)
    s.add(plan)
    await s.commit()
    return r, bot, cust, plan


async def _mk_order(s, cust, plan, bot, *, gb=10, days=30, price=30_000,  # noqa: ANN001
                    status="provisioned", uuid="uuid-1"):
    order = StorefrontOrder(
        customer_id=cust.id, plan_id=plan.id, panel_id=bot.panel_id, label="svc",
        gb=gb, days=days, price_toman=price, status=status, panel_user_uuid=uuid,
        sub_link=f"https://h/p/{uuid}/#x")
    s.add(order)
    await s.commit()
    return order


def _fake_renew(calls):  # noqa: ANN001, ANN202
    async def prepare(self, panel, uuid, *, gb, days, api_key=None):  # noqa: ANN001, ANN002
        return admin_api.RenewUserTarget(float(gb), int(days), "2026-06-01")

    async def apply(self, panel, uuid, target, *, api_key=None):  # noqa: ANN001, ANN002
        calls["renew"] = calls.get("renew", 0) + 1
    return prepare, apply


def test_prepare_renew_user_is_a_clean_reset(monkeypatch):
    """A renewal targets a FRESH plan — usage_limit = the plan (gb) and the consumed counter reset to
    ZERO — NOT the old cumulative `used + gb` that inflated the quota every cycle."""
    async def fake_get_user(self, panel, uuid, *, api_key=None):  # noqa: ANN001, ANN002
        return {"current_usage_GB": 45.0, "usage_limit_GB": 50.0, "start_date": "2026-07-01"}
    monkeypatch.setattr(admin_api.AdminApiClient, "get_user", fake_get_user)

    async def go():
        t = await admin_api.AdminApiClient().prepare_renew_user(object(), "uuid", gb=30, days=30)
        assert t.usage_limit_gb == 30.0          # the plan sold — NOT 45 + 30 = 75
        body = t.body
        assert body["usage_limit_GB"] == 30.0
        assert body["current_usage_GB"] == 0     # consumed counter reset → fresh 30 GB from zero
        assert body["package_days"] == 30
        assert body["start_date"] is None        # day-clock restarts on first connect
        assert t.prior_start_date == "2026-07-01"  # still captured for the durable reconciler
    asyncio.run(go())


async def _wallet(Session, cid):  # noqa: ANN001
    async with Session() as s:
        return int((await s.get(StorefrontCustomer, cid)).wallet_balance_toman)


async def _txn_count(Session, cid, kind):  # noqa: ANN001
    async with Session() as s:
        return (await s.execute(
            select(func.count()).select_from(StorefrontWalletTxn).where(
                StorefrontWalletTxn.customer_id == cid,
                StorefrontWalletTxn.kind == kind))).scalar_one()


# ─────────────────────────── wallet hold primitive ───────────────────────────
def test_place_hold_debits_and_release_credits_back(tmp_path):
    async def go():
        engine, Session = _engine_session(tmp_path, "hold1.db")
        await _create_all(engine)
        async with Session() as s:
            _r, _bot, cust, _plan = await _seed(s, balance=100_000)
            order = await _mk_order(s, cust, _plan, _bot)
            cid, oid = cust.id, order.id
            ok, hold = await storefront_wallet.place_hold(s, cid, 30_000, order_id=oid)
            await s.commit()
            assert ok and hold is not None
            hid = hold.id
        # balance dropped by the reserved price; a held txn exists
        assert await _wallet(Session, cid) == 70_000
        assert await _txn_count(Session, cid, "hold") == 1
        # release returns it and flips the hold off `held`
        async with Session() as s:
            back = await storefront_wallet.release_hold(s, hid)
            await s.commit()
            assert back is not None and back.status == "released"
        assert await _wallet(Session, cid) == 100_000
        # release is idempotent — a second call is a no-op (no double credit)
        async with Session() as s:
            assert await storefront_wallet.release_hold(s, hid) is None
            await s.commit()
        assert await _wallet(Session, cid) == 100_000
        await engine.dispose()
    asyncio.run(go())


def test_place_hold_refused_when_short(tmp_path):
    async def go():
        engine, Session = _engine_session(tmp_path, "hold2.db")
        await _create_all(engine)
        async with Session() as s:
            _r, _bot, cust, _plan = await _seed(s, balance=10_000)
            order = await _mk_order(s, cust, _plan, _bot)
            ok, hold = await storefront_wallet.place_hold(s, cust.id, 30_000, order_id=order.id)
            await s.commit()
            assert ok is False and hold is None
            cid = cust.id
        assert await _wallet(Session, cid) == 10_000   # untouched
        await engine.dispose()
    asyncio.run(go())


def test_settle_hold_relabels_to_purchase_without_touching_balance(tmp_path):
    async def go():
        engine, Session = _engine_session(tmp_path, "hold3.db")
        await _create_all(engine)
        async with Session() as s:
            _r, _bot, cust, _plan = await _seed(s, balance=100_000)
            order = await _mk_order(s, cust, _plan, _bot)
            _ok, hold = await storefront_wallet.place_hold(s, cust.id, 30_000, order_id=order.id)
            await s.commit()
            cid, hid = cust.id, hold.id
        async with Session() as s:
            settled = await storefront_wallet.settle_hold(s, hid, operation_id=None)
            await s.commit()
            assert settled is not None and settled.kind == "purchase" and settled.status == "done"
        # money was ALREADY out at hold time — settle must not move the balance again
        assert await _wallet(Session, cid) == 70_000
        # idempotent — already settled
        async with Session() as s:
            assert await storefront_wallet.settle_hold(s, hid) is None
            await s.commit()
        await engine.dispose()
    asyncio.run(go())


# ─────────────────────────── arm / disarm ───────────────────────────
def test_arm_reserves_price_and_disarm_returns_it(tmp_path):
    async def go():
        engine, Session = _engine_session(tmp_path, "arm1.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=100_000)
            order = await _mk_order(s, cust, plan, bot)
            oid, cid, sfid = order.id, cust.id, bot.id
        async with Session() as s:
            res = await storefront_autorenew.arm(s, oid, expected_sf_id=sfid)
            assert res.ok and res.price == 30_000
        assert await _wallet(Session, cid) == 70_000       # reserved
        async with Session() as s:
            o = await s.get(StorefrontOrder, oid)
            assert o.autorenew_armed_at is not None and o.autorenew_price_toman == 30_000
            assert o.autorenew_hold_txn_id is not None
        # re-arm is idempotent — no second hold
        async with Session() as s:
            res2 = await storefront_autorenew.arm(s, oid, expected_sf_id=sfid)
            assert res2.ok and res2.reason == "already"
        assert await _txn_count(Session, cid, "hold") == 1
        assert await _wallet(Session, cid) == 70_000
        # disarm returns the money + clears the columns
        async with Session() as s:
            await storefront_autorenew.disarm(s, oid, expected_sf_id=sfid)
        assert await _wallet(Session, cid) == 100_000
        async with Session() as s:
            o = await s.get(StorefrontOrder, oid)
            assert o.autorenew_armed_at is None and o.autorenew_hold_txn_id is None
        await engine.dispose()
    asyncio.run(go())


def test_arm_refused_when_balance_short(tmp_path):
    async def go():
        engine, Session = _engine_session(tmp_path, "arm2.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=10_000)
            order = await _mk_order(s, cust, plan, bot)
            oid, cid, sfid = order.id, cust.id, bot.id
        async with Session() as s:
            res = await storefront_autorenew.arm(s, oid, expected_sf_id=sfid)
            assert res.ok is False and res.reason == "insufficient" and res.short_toman == 20_000
        assert await _wallet(Session, cid) == 10_000
        async with Session() as s:
            assert (await s.get(StorefrontOrder, oid)).autorenew_armed_at is None
        await engine.dispose()
    asyncio.run(go())


def test_arm_refused_on_trial(tmp_path):
    async def go():
        engine, Session = _engine_session(tmp_path, "arm3.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s)
            order = await _mk_order(s, cust, plan, bot)
            order.is_trial = True
            await s.commit()
            oid, sfid = order.id, bot.id
        async with Session() as s:
            res = await storefront_autorenew.arm(s, oid, expected_sf_id=sfid)
            assert res.ok is False and res.reason == "trial"
        await engine.dispose()
    asyncio.run(go())


def test_delete_releases_the_hold(tmp_path):
    async def go():
        engine, Session = _engine_session(tmp_path, "arm4.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=100_000)
            order = await _mk_order(s, cust, plan, bot)
            oid, cid, sfid = order.id, cust.id, bot.id
        async with Session() as s:
            await storefront_autorenew.arm(s, oid, expected_sf_id=sfid)
        assert await _wallet(Session, cid) == 70_000
        # deleting the config must return the reserved money (no delete_user call needed; stub-free
        # path leaves uuid but the panel client would 404 — here we just assert the wallet math)
        from app.services.panel_client import admin_api as _aa

        async def _noop_delete(self, panel, uuid, *, api_key=None):  # noqa: ANN001, ANN002
            return None
        _orig = _aa.AdminApiClient.delete_user
        _aa.AdminApiClient.delete_user = _noop_delete
        try:
            res = await storefront_subscription.delete_subscription(
                Session, order_id=oid, expected_sf_id=sfid)
            assert res.ok
        finally:
            _aa.AdminApiClient.delete_user = _orig
        assert await _wallet(Session, cid) == 100_000   # reservation came back
        async with Session() as s:
            o = await s.get(StorefrontOrder, oid)
            assert o.status == "deleted" and o.autorenew_armed_at is None
        await engine.dispose()
    asyncio.run(go())


# ─────────────────────────── fire ───────────────────────────
def test_fire_settles_the_hold_and_is_one_shot(tmp_path, monkeypatch):
    calls: dict[str, int] = {}
    prepare, apply = _fake_renew(calls)
    monkeypatch.setattr(admin_api.AdminApiClient, "prepare_renew_user", prepare)
    monkeypatch.setattr(admin_api.AdminApiClient, "apply_renew_user_target", apply)

    async def go():
        engine, Session = _engine_session(tmp_path, "fire1.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=100_000)
            order = await _mk_order(s, cust, plan, bot)
            oid, cid, sfid = order.id, cust.id, bot.id
        async with Session() as s:
            assert (await storefront_autorenew.arm(s, oid, expected_sf_id=sfid)).ok
        assert await _wallet(Session, cid) == 70_000   # reserved at arm

        res = await storefront_autorenew.fire(Session, order_id=oid)
        assert res.ok, res
        assert calls.get("renew") == 1                  # panel renewed exactly once
        # the hold BECAME the payment — no SECOND debit; balance stays at the arm-time reservation
        assert await _wallet(Session, cid) == 70_000
        assert await _txn_count(Session, cid, "purchase") == 1
        assert await _txn_count(Session, cid, "hold") == 0   # relabelled to purchase
        # one-shot: the arm cleared
        async with Session() as s:
            o = await s.get(StorefrontOrder, oid)
            assert o.autorenew_armed_at is None and o.status == "provisioned"
            assert o.gb == 10 and o.days == 30

        # firing again is a no-op (not armed) — no extra renew / debit
        res2 = await storefront_autorenew.fire(Session, order_id=oid)
        assert res2.ok is False and res2.reason == "not_found"
        assert calls.get("renew") == 1
        assert await _wallet(Session, cid) == 70_000
        await engine.dispose()
    asyncio.run(go())


def test_fire_replay_same_arm_does_not_double_settle(tmp_path, monkeypatch):
    calls: dict[str, int] = {}
    prepare, apply = _fake_renew(calls)
    monkeypatch.setattr(admin_api.AdminApiClient, "prepare_renew_user", prepare)
    monkeypatch.setattr(admin_api.AdminApiClient, "apply_renew_user_target", apply)

    async def go():
        engine, Session = _engine_session(tmp_path, "fire2.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=100_000)
            order = await _mk_order(s, cust, plan, bot)
            oid, cid, sfid = order.id, cust.id, bot.id
            await storefront_autorenew.arm(s, oid, expected_sf_id=sfid)
            hid = (await s.get(StorefrontOrder, oid)).autorenew_hold_txn_id
        # Fire, then re-fire with the SAME deterministic op_id (simulate a retry) directly through
        # renew() — the op REPLAYs, so no second settle/charge.
        op_id = f"autorenew:{oid}:{hid}"
        r1 = await storefront_subscription.renew(
            Session, order_id=oid, op_id=op_id, prepaid_hold_txn_id=hid, locked_price_toman=30_000)
        assert r1.ok
        r2 = await storefront_subscription.renew(
            Session, order_id=oid, op_id=op_id, prepaid_hold_txn_id=hid, locked_price_toman=30_000)
        assert r2.ok
        assert calls.get("renew") == 1
        assert await _txn_count(Session, cid, "purchase") == 1
        assert await _wallet(Session, cid) == 70_000
        await engine.dispose()
    asyncio.run(go())


def test_sweep_backstop_clears_dangling_arm(tmp_path):
    """An order left `armed` but whose hold is no longer `held` (a reconciler-completed / crashed
    fire) is cleaned up by the sweep, with no fire."""
    async def go():
        engine, Session = _engine_session(tmp_path, "fire3.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=100_000)
            order = await _mk_order(s, cust, plan, bot)
            await storefront_autorenew.arm(s, order.id, expected_sf_id=bot.id)
            oid = order.id
            # Simulate the hold already settled (fire done) but armed columns not yet cleared.
            o = await s.get(StorefrontOrder, oid)
            await storefront_wallet.settle_hold(s, o.autorenew_hold_txn_id)
            await s.commit()
        res = await storefront_autorenew.sweep(Session)
        assert res["backstop_cleared"] == 1 and res["fired"] == 0
        async with Session() as s:
            assert (await s.get(StorefrontOrder, oid)).autorenew_armed_at is None
        await engine.dispose()
    asyncio.run(go())


# ─────────────────────────── PG barrier: arm/fire/disarm never double-spend ───────────────────────────
@pytest.mark.pg_contract
@requires_pg
def test_concurrent_fire_and_disarm_settle_or_release_once(monkeypatch):
    calls: dict[str, int] = {}
    prepare, apply = _fake_renew(calls)
    monkeypatch.setattr(admin_api.AdminApiClient, "prepare_renew_user", prepare)
    monkeypatch.setattr(admin_api.AdminApiClient, "apply_renew_user_target", apply)

    async def run():
        engine, Session = make_engine()
        try:
            async with Session() as s:
                _r, bot, cust, plan = await _seed(s, balance=100_000)
                order = await _mk_order(s, cust, plan, bot)
                oid, cid, sfid = order.id, cust.id, bot.id
                pid, rid = bot.panel_id, bot.reseller_id
                await storefront_autorenew.arm(s, oid, expected_sf_id=sfid)

            # Fire and disarm race on the SAME armed order. The order row lock serializes them, so
            # exactly ONE of {settle-as-purchase, release} wins — never both, never a double debit.
            async def _disarm():
                async with Session() as s2:
                    return await storefront_autorenew.disarm(s2, oid, expected_sf_id=sfid)

            a, b = await asyncio.gather(
                storefront_autorenew.fire(Session, order_id=oid), _disarm(),
                return_exceptions=True)
            assert not isinstance(a, BaseException), a
            assert not isinstance(b, BaseException), b

            purchases = await _txn_count(Session, cid, "purchase")
            bal = await _wallet(Session, cid)
            # Either the fire won (1 purchase, balance stays at the reservation) or disarm won
            # (0 purchase, balance fully restored). Never a half state.
            assert (purchases, bal) in {(1, 70_000), (0, 100_000)}, (purchases, bal, a, b)
            async with Session() as s:
                for model in (StorefrontWalletTxn, StorefrontOperation, StorefrontOrder,
                              StorefrontCustomer, StorefrontPlan, StorefrontBot):
                    await s.execute(model.__table__.delete())
                await s.execute(Reseller.__table__.delete().where(Reseller.id == rid))
                await s.execute(Panel.__table__.delete().where(Panel.id == pid))
                await s.commit()
        finally:
            await engine.dispose()
    asyncio.run(run())


# ───────────────── a plan sold below the reseller's own cost never renews ─────────────────

def test_arming_a_below_cost_plan_is_refused_before_any_money_is_reserved(tmp_path):
    """A shop selling 10 GB for 50 T against a 10,000 T cost must not lock the customer's money
    into a renewal that would deepen the reseller's loss every cycle."""
    async def go():
        engine, Session = _engine_session(tmp_path, "bc-arm.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=100_000, price=50)
            order = await _mk_order(s, cust, plan, bot, price=50)
            res = await storefront_autorenew.arm(s, order.id, expected_sf_id=bot.id)
            assert res.ok is False and res.reason == "below_cost"
            cid, oid = cust.id, order.id
        assert await _wallet(Session, cid) == 100_000        # nothing reserved
        assert await _txn_count(Session, cid, "hold") == 0
        async with Session() as s:
            assert (await s.get(StorefrontOrder, oid)).autorenew_armed_at is None
        await engine.dispose()
    asyncio.run(go())


def test_a_below_cost_renewal_is_refused_before_any_debit(tmp_path):
    async def go():
        engine, Session = _engine_session(tmp_path, "bc-renew.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=100_000, price=50)
            order = await _mk_order(s, cust, plan, bot, price=50)
            cid, oid = cust.id, order.id
        res = await storefront_subscription.renew(Session, order_id=oid, expected_sf_id=bot.id)
        assert res.ok is False and res.reason == "below_cost"
        assert await _wallet(Session, cid) == 100_000
        async with Session() as s:
            assert (await s.execute(
                select(func.count()).select_from(StorefrontOperation))).scalar_one() == 0
        await engine.dispose()
    asyncio.run(go())


def test_repricing_a_disabled_plan_above_cost_makes_it_renewable_again(tmp_path):
    """The price and the quota must share one vintage. Renewal has always read gb from the LIVE
    plan; taking the price from the ORDER whenever the plan was disabled meant a plan disabled for
    underpricing kept renewing at the old loss-making price and a fix could never reach it."""
    async def go():
        engine, Session = _engine_session(tmp_path, "bc-vintage.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=100_000, price=50)
            order = await _mk_order(s, cust, plan, bot, price=50)
            oid, pid = order.id, plan.id
        # Disabled AND still below cost → refused.
        async with Session() as s:
            p = await s.get(StorefrontPlan, pid)
            p.enabled = False
            await s.commit()
        assert (await storefront_subscription.renew(
            Session, order_id=oid, expected_sf_id=bot.id)).reason == "below_cost"
        # The reseller fixes the price. The (still disabled) plan now governs the renewal.
        async with Session() as s:
            p = await s.get(StorefrontPlan, pid)
            p.price_toman = 50_000
            await s.commit()
        res = await storefront_subscription.renew(
            Session, order_id=oid, by_admin=True, expected_sf_id=bot.id)
        assert res.reason != "below_cost"
        await engine.dispose()
    asyncio.run(go())


def test_the_sweep_refunds_an_armed_order_whose_plan_turned_out_below_cost(tmp_path):
    """Unlike a suspension (transient — kept armed so it resumes on payment), a below-cost price may
    never be fixed. Holding the customer's money for a renewal that can never fire is worse than
    refunding it, so the arm is released and the customer told."""
    async def go():
        engine, Session = _engine_session(tmp_path, "bc-sweep.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=100_000, price=30_000)
            order = await _mk_order(s, cust, plan, bot)
            armed = await storefront_autorenew.arm(s, order.id, expected_sf_id=bot.id)
            assert armed.ok
            cid, oid = cust.id, order.id
        assert await _wallet(Session, cid) == 70_000     # reserved while the price was healthy

        # The reseller then slashes the price under their own cost.
        async with Session() as s:
            p = await s.get(StorefrontPlan, plan.id)
            p.price_toman = 50
            o = await s.get(StorefrontOrder, oid)
            o.autorenew_price_toman = 50
            await s.commit()

        sent: list[str] = []

        class _Bot:
            session = SimpleNamespace(close=lambda: asyncio.sleep(0))

            async def send_message(self, chat_id, text, **kw):  # noqa: ANN001, ANN003, ARG002
                sent.append(text)

        async def bot_factory(_token):  # noqa: ANN001, ANN202
            return _Bot()

        res = await storefront_autorenew.sweep(Session, bot_factory=bot_factory)
        assert res["below_cost_disarmed"] == 1 and res["fired"] == 0
        assert await _wallet(Session, cid) == 100_000    # the reservation came back
        async with Session() as s:
            assert (await s.get(StorefrontOrder, oid)).autorenew_armed_at is None
        assert sent and "به کیفِ پولِ شما بازگشت" in sent[0]
        # ...and the reseller's own buy price is never disclosed to a customer.
        assert "هزینه" not in sent[0]
        await engine.dispose()
    asyncio.run(go())


def test_an_admin_renew_of_a_below_cost_plan_is_a_definite_refusal_not_an_ambiguous_one(
        tmp_path, monkeypatch):
    """`below_cost` is decided before any durable state or panel I/O, so it must surface as a 422
    validation failure — not the `external_unknown` 502 that hands the order to the reaper."""
    async def go():
        engine, Session = _engine_session(tmp_path, "bc-admin.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=100_000, price=50)
            order = await _mk_order(s, cust, plan, bot, price=50)
            oid, sid = order.id, bot.id

        # renew_order dispatches through the app-level SessionLocal; point it at this test DB.
        monkeypatch.setattr(storefront_admin, "SessionLocal", Session)
        async with Session() as s:
            ctx = storefront_admin.CommandContext(
                actor_telegram_id=0, actor_role="system", source="system",
                idempotency_key="renew-bc", expected_version=1)
            with pytest.raises(storefront_admin.AdminCommandError) as exc:
                await storefront_admin.renew_order(s, sid, oid, ctx)
            assert exc.value.response_status == 422
            assert exc.value.code != "external_unknown"
            assert "50000" in str(exc.value)          # the admin still gets the unit lesson
            event = (await s.execute(select(StorefrontAuditEvent))).scalars().all()[-1]
            assert event.outcome == "failed" and event.error_class == "below_cost"
        await engine.dispose()
    asyncio.run(go())
