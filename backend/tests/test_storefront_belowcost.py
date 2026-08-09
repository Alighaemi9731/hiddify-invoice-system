"""A storefront plan may never be sold below the reseller's own cost.

Covers the guard on all three write paths, the Persian message that teaches the ×1000 unit rule,
and the one-shot sweep that remediates shops priced that way before the guard existed.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models import (
    DeliveryLog,
    Panel,
    Reseller,
    StorefrontApiCommand,
    StorefrontAuditEvent,
    StorefrontBot,
    StorefrontPlan,
)
from app.models.enums import DeliveryStatus
from app.services import (
    settings_service,
    storefront,
    storefront_admin,
    storefront_belowcost,
    storefront_pricing,
)

DEFAULT_COST = 1000   # `default_price_per_gb`, registered by `_seed` below


def _run(body):  # noqa: ANN001, ANN202
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                await body(session)
        finally:
            await engine.dispose()

    asyncio.run(go())


async def _seed(session, *, price_per_gb=None, chat_id=111):  # noqa: ANN001, ANN202
    # Pin the global price these cases compute against, so the shipped default can move
    # without rewriting every expected floor here.
    await settings_service.set_value(session, "default_price_per_gb", DEFAULT_COST)
    panel = Panel(key="p1", host="p1.invalid", proxy_path_enc="secret", owner_uuid="owner")
    session.add(panel)
    await session.flush()
    reseller = Reseller(
        panel_id=panel.id, admin_uuid="r1", name="Owner", bot_chat_id=chat_id,
        storefront_enabled=True, price_per_gb=price_per_gb,
    )
    session.add(reseller)
    await session.flush()
    shop = StorefrontBot(
        reseller_id=reseller.id, panel_id=panel.id, bot_token_enc="token",
        bot_username="shop_bot", config_version=1, pay_card_enabled=False,
    )
    session.add(shop)
    await session.commit()
    return reseller, shop


def _ctx(version: int, key: str = "k1", *, actor: int = 111, source="portal"):  # noqa: ANN001, ANN202
    return storefront_admin.CommandContext(
        actor_telegram_id=actor, actor_role="owner", source=source,
        idempotency_key=key, expected_version=version,
    )


async def _plan(session, shop, *, gb=10, days=30, price, key="seed"):  # noqa: ANN001, ANN202
    """Insert a plan directly, bypassing the guard — the sweep exists precisely because such rows
    already exist in production from before the guard did."""
    plan = StorefrontPlan(
        storefront_bot_id=shop.id, title="", gb=gb, days=days, price_toman=price,
        enabled=True, sort_order=0,
    )
    session.add(plan)
    await session.commit()
    return plan


# ── the pure rule ────────────────────────────────────────────────────────────

def test_floor_is_inclusive_and_the_hint_teaches_the_unit():
    # Selling at exactly cost is the reseller's call — only strictly below is refused.
    assert storefront_pricing.is_below_cost(cost=2000, gb=10, price_toman=20_000) is False
    assert storefront_pricing.is_below_cost(cost=2000, gb=10, price_toman=19_999) is True
    # A free plan is below cost too: `price_toman=0` was one of the real production shapes.
    assert storefront_pricing.is_below_cost(cost=2000, gb=10, price_toman=0) is True
    # No cost configured → the guard is inert rather than blocking every plan.
    assert storefront_pricing.is_below_cost(cost=0, gb=10, price_toman=0) is False

    msg = storefront_pricing.below_cost_message_fa(cost=2000, gb=10, price_toman=50)
    assert "50000" in msg                      # the copy-pasteable example
    assert "عددِ درست این است: 50000" in msg    # ...and the user's OWN number, x1000
    assert "20,000" in msg                     # the floor they must clear
    # A number that x1000 still would not clear must not be "suggested" — it would just be
    # rejected again.
    assert storefront_pricing.suggested_price(cost=2000, gb=10, price_toman=1) is None
    assert "عددی برابر یا بیشتر از 20000" in storefront_pricing.below_cost_message_fa(
        cost=2000, gb=10, price_toman=1)


# ── the guard on every write path ────────────────────────────────────────────

def test_create_update_and_enable_all_refuse_a_below_cost_plan():
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session)

        with pytest.raises(storefront_admin.AdminCommandError) as created:
            await storefront_admin.create_plan(
                session, shop.id, _ctx(1, "create"), gb=10, days=30, price_toman=50)
        assert created.value.code == "below_cost"
        assert created.value.response_status == 422
        assert created.value.response_body["min_price_toman"] == 10 * DEFAULT_COST
        assert created.value.response_body["suggested_price_toman"] == 50_000
        assert "50000" in str(created.value)
        assert await session.scalar(select(func.count(StorefrontPlan.id))) == 0
        # A refused command must not burn the shop's config version.
        assert await session.scalar(select(StorefrontBot.config_version)) == 1

        ok = await storefront_admin.create_plan(
            session, shop.id, _ctx(1, "ok"), gb=10, days=30, price_toman=10_000)
        plan_id = ok.body["plan"]["id"]

        with pytest.raises(storefront_admin.AdminCommandError) as updated:
            await storefront_admin.update_plan(
                session, shop.id, plan_id, _ctx(2, "update"), price_toman=99)
        assert updated.value.code == "below_cost"

        # Raising gb alone can push an unchanged price under the floor.
        with pytest.raises(storefront_admin.AdminCommandError) as widened:
            await storefront_admin.update_plan(
                session, shop.id, plan_id, _ctx(2, "widen"), gb=50)
        assert widened.value.code == "below_cost"

    _run(body)


def test_a_below_cost_plan_can_be_disabled_but_not_re_enabled():
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        plan = await _plan(session, shop, price=50)

        # Disabling must ALWAYS work — it is the remedy, and the sweep depends on it.
        off = await storefront_admin.set_plan_enabled(
            session, shop.id, plan.id, _ctx(1, "off"), enabled=False)
        assert off.body["plan"]["enabled"] is False

        with pytest.raises(storefront_admin.AdminCommandError) as on:
            await storefront_admin.set_plan_enabled(
                session, shop.id, plan.id, _ctx(2, "on"), enabled=True)
        assert on.value.code == "below_cost"
        assert "فعال نشد" in str(on.value)
        await session.refresh(plan)
        assert plan.enabled is False

        # Repricing above the floor makes it enableable again.
        await storefront_admin.update_plan(
            session, shop.id, plan.id, _ctx(2, "fix"), price_toman=50_000)
        back = await storefront_admin.set_plan_enabled(
            session, shop.id, plan.id, _ctx(3, "on2"), enabled=True)
        assert back.body["plan"]["enabled"] is True

    _run(body)


def test_a_refused_plan_is_recorded_as_a_finalized_failure_not_left_in_flight():
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        with pytest.raises(storefront_admin.AdminCommandError):
            await storefront_admin.create_plan(
                session, shop.id, _ctx(1, "dup"), gb=10, days=30, price_toman=50)
        command = (await session.execute(select(StorefrontApiCommand))).scalars().one()
        assert command.status == "failed"
        assert command.response_status == 422
        event = (await session.execute(select(StorefrontAuditEvent))).scalars().one()
        assert event.outcome == "failed" and event.error_class == "below_cost"

    _run(body)


def test_the_resellers_own_price_beats_the_global_default():
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session, price_per_gb=5000)
        # 10 GB x 5000 = 50,000 — a price fine under the 1000 default is refused here.
        with pytest.raises(storefront_admin.AdminCommandError) as exc:
            await storefront_admin.create_plan(
                session, shop.id, _ctx(1, "c"), gb=10, days=30, price_toman=20_000)
        assert exc.value.response_body["min_price_toman"] == 50_000

    _run(body)


def test_a_zero_price_per_gb_falls_through_to_the_default():
    """Pins the inherited `x or default` quirk shared with invoicing: a reseller row carrying 0 is
    billed the global default, so the guard must use that same number or it would disagree with
    what the reseller is actually charged."""
    async def body(session):  # noqa: ANN001
        reseller, _shop = await _seed(session, price_per_gb=0)
        assert await storefront_pricing.cost_per_gb(session, reseller) == DEFAULT_COST

    _run(body)


def test_a_system_sourced_command_needs_no_actor_and_is_audited_as_system():
    """The sweep's authorization path — untested before it had a caller."""
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        plan = await _plan(session, shop, price=50)
        await storefront_admin.set_plan_enabled(
            session, shop.id, plan.id,
            storefront_admin.CommandContext(
                actor_telegram_id=0, actor_role="system", source="system",
                idempotency_key="sweep:1", expected_version=1),
            enabled=False,
        )
        event = (
            await session.execute(
                select(StorefrontAuditEvent).where(StorefrontAuditEvent.outcome == "succeeded"))
        ).scalars().one()
        assert event.source == "system" and event.actor_role == "system"

    _run(body)


# ── the sweep ────────────────────────────────────────────────────────────────

class _StubBot:
    """Stands in for the main bot: records what each reseller was told."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail
        self.session = _StubSession()

    async def send_message(self, chat_id, text):  # noqa: ANN001, ANN202
        if self.fail:
            raise RuntimeError("telegram unreachable")
        self.sent.append((chat_id, text))


class _StubSession:
    async def close(self) -> None:
        return None


def test_scan_reports_only_enabled_below_cost_plans():
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        await _plan(session, shop, gb=10, price=50)          # below cost
        await _plan(session, shop, gb=10, price=10_000)      # exactly at cost → fine
        disabled = await _plan(session, shop, gb=5, price=1)
        disabled.enabled = False
        await session.commit()

        findings = await storefront_belowcost.scan(session)
        assert len(findings) == 1
        assert [p.price_toman for p in findings[0].below] == [50]
        assert findings[0].enabled_ok == 1
        assert findings[0].will_close is False   # a healthy plan remains

    _run(body)


def test_a_dry_run_changes_nothing():
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        plan = await _plan(session, shop, price=50)
        bot = _StubBot()

        result = await storefront_belowcost.run_sweep(session, dry_run=True, bot=bot)
        assert result["would_disable"] == 1 and result["would_close"] == 1

        await session.refresh(plan)
        await session.refresh(shop)
        assert plan.enabled is True
        assert shop.shop_closed is False
        assert shop.config_version == 1
        assert bot.sent == []
        assert await session.scalar(select(func.count(DeliveryLog.id))) == 0
        assert await settings_service.get(session, storefront_belowcost.STAMP_KEY, None) is None

    _run(body)


def test_the_sweep_disables_every_plan_of_a_shop_across_repeated_version_bumps():
    """Each successful disable bumps `config_version`, so a cached version would 409 on the
    second plan — the whole reason the sweep re-reads it per call."""
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        for _ in range(3):
            await _plan(session, shop, price=50)
        bot = _StubBot()

        result = await storefront_belowcost.run_sweep(session, dry_run=False, bot=bot)
        assert result["disabled"] == 3
        assert result["failed"] == 0

        remaining = await storefront.list_plans(session, shop.id, only_enabled=True)
        assert remaining == []
        await session.refresh(shop)
        assert shop.config_version > 3

    _run(body)


def test_a_shop_with_nothing_left_to_sell_is_closed_with_an_explanation():
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        await _plan(session, shop, price=50)
        bot = _StubBot()

        await storefront_belowcost.run_sweep(session, dry_run=False, bot=bot)
        await session.refresh(shop)
        assert shop.shop_closed is True
        assert shop.closed_text == storefront_belowcost.CLOSED_TEXT_FA
        # Customer-facing text must never disclose the reseller's buy price.
        assert "هزینه" not in shop.closed_text

    _run(body)


def test_a_shop_that_still_has_a_healthy_plan_stays_open():
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        await _plan(session, shop, gb=10, price=50)
        await _plan(session, shop, gb=10, price=50_000)
        bot = _StubBot()

        result = await storefront_belowcost.run_sweep(session, dry_run=False, bot=bot)
        assert result["disabled"] == 1 and result["closed"] == 0
        await session.refresh(shop)
        assert shop.shop_closed is False

    _run(body)


def test_a_reseller_who_closed_their_own_shop_keeps_their_own_message():
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        shop.shop_closed = True
        shop.closed_text = "به‌زودی برمی‌گردیم"
        await session.commit()
        await _plan(session, shop, price=50)

        await storefront_belowcost.run_sweep(session, dry_run=False, bot=_StubBot())
        await session.refresh(shop)
        assert shop.closed_text == "به‌زودی برمی‌گردیم"

    _run(body)


def test_the_reseller_is_told_what_happened_and_how_to_fix_it():
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        await _plan(session, shop, gb=10, price=50)
        bot = _StubBot()

        await storefront_belowcost.run_sweep(session, dry_run=False, bot=bot)
        assert len(bot.sent) == 1
        chat_id, text = bot.sent[0]
        assert chat_id == 111
        assert "50000" in text          # the unit lesson
        assert "10,000" in text         # the floor for that plan
        assert "بسته" in text            # ...and that the shop was closed
        log = (await session.execute(select(DeliveryLog))).scalars().one()
        assert log.status == DeliveryStatus.sent

    _run(body)


def test_re_running_the_sweep_is_a_no_op_and_does_not_notify_twice():
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        await _plan(session, shop, price=50)
        bot = _StubBot()

        await storefront_belowcost.run_sweep(session, dry_run=False, bot=bot)
        await session.refresh(shop)
        version_after_first = shop.config_version

        again = await storefront_belowcost.run_sweep(session, dry_run=False, bot=bot)
        assert again["shops"] == 0 and again["disabled"] == 0
        assert len(bot.sent) == 1
        await session.refresh(shop)
        assert shop.config_version == version_after_first

    _run(body)


def test_a_transient_delivery_failure_is_retried_but_a_block_is_not():
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        await _plan(session, shop, price=50)

        failing = _StubBot(fail=True)
        await storefront_belowcost.run_sweep(session, dry_run=False, bot=failing)
        stamp = await settings_service.get(session, storefront_belowcost.STAMP_KEY, {})
        assert stamp["shops"][str(shop.id)].get("notified_at") is None

        # The plans are already disabled, so `scan` no longer sees them — the retry must still be
        # able to describe what went dark, from the sweep's own record.
        working = _StubBot()
        retried = await storefront_belowcost.retry_pending_notices(session, bot=working)
        assert retried["sent"] == 1
        assert "50000" in working.sent[0][1]

        # Once delivered, it is never re-sent.
        third = _StubBot()
        assert (await storefront_belowcost.retry_pending_notices(session, bot=third))["pending"] == 0
        assert third.sent == []

    _run(body)


def test_retry_never_disables_anything_even_when_the_default_price_rises():
    """The safety property that lets this run on a schedule: raising `default_price_per_gb` must
    not turn the daily retry into an unattended mass-disable of healthy shops."""
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        healthy = await _plan(session, shop, gb=10, price=50_000)
        await settings_service.set_value(session, "default_price_per_gb", 100_000)

        result = await storefront_belowcost.retry_pending_notices(session, bot=_StubBot())
        assert result["pending"] == 0
        await session.refresh(healthy)
        assert healthy.enabled is True
        await session.refresh(shop)
        assert shop.shop_closed is False

    _run(body)


def test_a_shop_whose_reseller_never_registered_is_still_remediated():
    async def body(session):  # noqa: ANN001
        reseller, shop = await _seed(session)
        reseller.bot_chat_id = None
        await session.commit()
        plan = await _plan(session, shop, price=50)

        result = await storefront_belowcost.run_sweep(session, dry_run=False, bot=_StubBot())
        assert result["disabled"] == 1
        await session.refresh(plan)
        assert plan.enabled is False
        log = (await session.execute(select(DeliveryLog))).scalars().one()
        assert log.status == DeliveryStatus.unmatched   # definitive → not retried forever

    _run(body)


def test_the_report_totals_the_exposure_without_touching_anything():
    async def body(session):  # noqa: ANN001
        _reseller, shop = await _seed(session)
        await _plan(session, shop, gb=10, price=50)
        await _plan(session, shop, gb=20, price=100)

        data = await storefront_belowcost.report(session)
        assert data["shops_affected"] == 1
        assert data["plans_affected"] == 2
        assert data["shops_would_close"] == 1
        # (10,000 - 50) + (20,000 - 100)
        assert data["uncovered_toman"] == 29_850
        assert await session.scalar(select(StorefrontBot.config_version)) == 1

    _run(body)
