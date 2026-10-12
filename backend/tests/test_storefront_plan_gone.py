"""A storefront order whose plan the shop DELETED is not renewable by the customer (v1.123.5).

Deleting a plan `SET NULL`s `storefront_orders.plan_id`, and renewal used to fall back to the
order's purchase-time `price_toman` — forever. Shop 25 replaced its 200k plan with a 250k one and
kept renewing every existing customer at 200k with no way to reprice them. Now the customer is told
the plan is no longer sold and pointed at the current plans; `storefront_plan_relink` re-attaches the
orders that have an unambiguous live twin so they renew at the shop's current price.

SQLite does not enforce the FK, so "the plan was deleted" is modelled as `plan_id = None` — exactly
the state Postgres's `ON DELETE SET NULL` leaves behind.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/sfplangone.db")
os.environ.setdefault("SECRET_KEY", "k")

from app.bot.storefront import keyboards as kb  # noqa: E402
from app.models import StorefrontOrder, StorefrontPlan  # noqa: E402
from app.services import (  # noqa: E402
    storefront,
    storefront_autorenew,
    storefront_expiry,
    storefront_plan_relink,
    storefront_subscription,
)
from app.services.panel_client import admin_api  # noqa: E402
from tests.test_storefront_autorenew import (  # noqa: E402
    _create_all,
    _engine_session,
    _fake_renew,
    _mk_order,
    _seed,
    _txn_count,
    _wallet,
)


async def _orphan(Session, oid):  # noqa: ANN001
    async with Session() as s:
        (await s.get(StorefrontOrder, oid)).plan_id = None
        await s.commit()


def _callbacks(markup) -> list[str]:  # noqa: ANN001
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_arming_an_order_whose_plan_was_deleted_is_refused_and_reserves_nothing(tmp_path):
    async def go():
        engine, Session = _engine_session(tmp_path, "pg-arm.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=100_000)
            order = await _mk_order(s, cust, plan, bot, price=20_000)
            oid, cid, sfid = order.id, cust.id, bot.id
        await _orphan(Session, oid)
        async with Session() as s:
            res = await storefront_autorenew.arm(s, oid, expected_sf_id=sfid)
        assert res.ok is False and res.reason == "plan_gone"
        assert await _wallet(Session, cid) == 100_000
        assert await _txn_count(Session, cid, "hold") == 0
        await engine.dispose()
    asyncio.run(go())


def test_a_customer_renewal_of_a_deleted_plan_is_refused_before_any_debit_or_panel_call(
        tmp_path, monkeypatch):
    """The old fallback renewed at the purchase-time price; nothing may charge or touch the panel."""
    calls: dict[str, int] = {}
    prepare, apply = _fake_renew(calls)
    monkeypatch.setattr(admin_api.AdminApiClient, "prepare_renew_user", prepare)
    monkeypatch.setattr(admin_api.AdminApiClient, "apply_renew_user_target", apply)

    async def go():
        engine, Session = _engine_session(tmp_path, "pg-renew.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=100_000)
            order = await _mk_order(s, cust, plan, bot, price=20_000)
            oid, cid = order.id, cust.id
        await _orphan(Session, oid)
        res = await storefront_subscription.renew(Session, order_id=oid)
        assert res.ok is False and res.reason == "plan_gone"
        assert calls.get("renew") is None
        assert await _wallet(Session, cid) == 100_000
        assert await _txn_count(Session, cid, "purchase") == 0
        await engine.dispose()
    asyncio.run(go())


def test_an_admin_free_renewal_still_works_without_the_plan(tmp_path, monkeypatch):
    """A shop admin's renewal is a free grant of the order's own gb/days — no price to get wrong."""
    calls: dict[str, int] = {}
    prepare, apply = _fake_renew(calls)
    monkeypatch.setattr(admin_api.AdminApiClient, "prepare_renew_user", prepare)
    monkeypatch.setattr(admin_api.AdminApiClient, "apply_renew_user_target", apply)

    async def go():
        engine, Session = _engine_session(tmp_path, "pg-admin.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=100_000)
            order = await _mk_order(s, cust, plan, bot, gb=25, days=30, price=200_000)
            oid, cid = order.id, cust.id
        await _orphan(Session, oid)
        res = await storefront_subscription.renew(Session, order_id=oid, by_admin=True)
        assert res.ok and res.gb == 25 and res.days == 30 and res.price == 0
        assert calls.get("renew") == 1
        assert await _wallet(Session, cid) == 100_000
        await engine.dispose()
    asyncio.run(go())


def test_the_sweep_refunds_an_armed_order_whose_plan_was_deleted_and_points_at_the_plans(tmp_path):
    async def go():
        engine, Session = _engine_session(tmp_path, "pg-sweep.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=100_000, price=30_000)
            order = await _mk_order(s, cust, plan, bot)
            assert (await storefront_autorenew.arm(s, order.id, expected_sf_id=bot.id)).ok
            cid, oid = cust.id, order.id
        assert await _wallet(Session, cid) == 70_000
        await _orphan(Session, oid)   # the shop deletes the plan after the customer armed

        sent: list[tuple[str, object]] = []

        class _Bot:
            session = SimpleNamespace(close=lambda: asyncio.sleep(0))

            async def send_message(self, chat_id, text, **kw):  # noqa: ANN001, ANN003, ARG002
                sent.append((text, kw.get("reply_markup")))

        async def bot_factory(_token):  # noqa: ANN001, ANN202
            return _Bot()

        res = await storefront_autorenew.sweep(Session, bot_factory=bot_factory)
        assert res["plan_gone_disarmed"] == 1 and res["fired"] == 0
        assert await _wallet(Session, cid) == 100_000
        async with Session() as s:
            assert (await s.get(StorefrontOrder, oid)).autorenew_armed_at is None
        assert len(sent) == 1
        text, markup = sent[0]
        assert "دیگر در فروشگاه ارائه نمی‌شود" in text
        assert _callbacks(markup) == ["sfbuylist"]
        await engine.dispose()
    asyncio.run(go())


def test_expiry_usage_and_winback_notices_point_a_plan_less_order_at_the_current_plans():
    for text in (storefront_expiry._message_fa("x", 2, plan_gone=True),
                 storefront_expiry._expired_msg("x", 1, plan_gone=True),
                 storefront_expiry._usage_high_msg("x", 9, 10, plan_gone=True)):
        assert storefront_expiry.PLAN_GONE_CTA_FA in text
        assert "«تمدید خودکار» را روشن کنید" not in text
    for text in (storefront_expiry._message_fa("x", 2),
                 storefront_expiry._expired_msg("x", 1),
                 storefront_expiry._usage_high_msg("x", 9, 10)):
        assert "«تمدید خودکار» را روشن کنید" in text
    assert storefront_expiry._plan_gone(SimpleNamespace(plan_id=None))
    assert not storefront_expiry._plan_gone(SimpleNamespace(plan_id=7))


def test_the_service_card_offers_buying_a_current_plan_instead_of_auto_renew():
    gone = _callbacks(kb.order_actions_kb(5, 200_000, paused=False, renewable=False))
    assert "sfbuylist" in gone and "sfauto:5" not in gone
    # a still-armed order keeps its «لغو», or the customer could never take the reservation back
    armed = _callbacks(kb.order_actions_kb(5, 200_000, paused=False, renewable=False, armed=True))
    assert "sfauto:5" in armed
    live = _callbacks(kb.order_actions_kb(5, 200_000, paused=False))
    assert "sfauto:5" in live and "sfbuylist" not in live


def test_relink_attaches_only_unambiguous_enabled_twins_and_dry_run_writes_nothing(tmp_path):
    async def go():
        engine, Session = _engine_session(tmp_path, "pg-relink.db")
        await _create_all(engine)
        async with Session() as s:
            _r, bot, cust, plan = await _seed(s, balance=0, price=30_000)   # 10 GB / 30 d
            new25 = StorefrontPlan(storefront_bot_id=bot.id, gb=25, days=30, price_toman=250_000,
                                   enabled=True)
            off50 = StorefrontPlan(storefront_bot_id=bot.id, gb=50, days=30, price_toman=470_000,
                                   enabled=False)
            dup_a = StorefrontPlan(storefront_bot_id=bot.id, gb=60, days=60, price_toman=500_000,
                                   enabled=True)
            dup_b = StorefrontPlan(storefront_bot_id=bot.id, gb=60, days=60, price_toman=530_000,
                                   enabled=True)
            s.add_all([new25, off50, dup_a, dup_b])
            await s.commit()
            o25 = await _mk_order(s, cust, plan, bot, gb=25, days=30, price=200_000, uuid="u1")
            o50 = await _mk_order(s, cust, plan, bot, gb=50, days=30, price=400_000, uuid="u2")
            o60 = await _mk_order(s, cust, plan, bot, gb=60, days=60, price=450_000, uuid="u3")
            gone = await _mk_order(s, cust, plan, bot, gb=25, days=30, price=200_000,
                                   status="deleted", uuid="u4")
            trial = await _mk_order(s, cust, plan, bot, gb=25, days=30, price=0, uuid="u5")
            trial.is_trial = True
            await s.commit()
            ids = {k: o.id for k, o in
                   {"o25": o25, "o50": o50, "o60": o60, "gone": gone, "trial": trial}.items()}
            new25_id = new25.id
        for oid in ids.values():
            await _orphan(Session, oid)

        async with Session() as s:
            dry = await storefront_plan_relink.relink(s)
        assert dry["dry_run"] and dry["candidates"] == 3
        assert (dry["relinked"], dry["unmatched"], dry["ambiguous"], dry["price_up"]) == (1, 1, 1, 1)
        async with Session() as s:
            assert (await s.get(StorefrontOrder, ids["o25"])).plan_id is None   # nothing written

        async with Session() as s:
            applied = await storefront_plan_relink.relink(s, dry_run=False)
        assert applied["relinked"] == 1
        async with Session() as s:
            assert (await s.get(StorefrontOrder, ids["o25"])).plan_id == new25_id
            for k in ("o50", "o60", "gone", "trial"):   # disabled twin / ambiguous / terminal / trial
                assert (await s.get(StorefrontOrder, ids[k])).plan_id is None
        # ...and the relinked order now arms at the shop's CURRENT price.
        async with Session() as s:
            c = await storefront.get_or_create_customer(
                s, (await s.get(StorefrontPlan, new25_id)).storefront_bot_id,
                SimpleNamespace(id=555, first_name="Cust", username="c"))
            c.wallet_balance_toman = 300_000
            await s.commit()
        async with Session() as s:
            res = await storefront_autorenew.arm(s, ids["o25"])
        assert res.ok and res.price == 250_000
        await engine.dispose()
    asyncio.run(go())
