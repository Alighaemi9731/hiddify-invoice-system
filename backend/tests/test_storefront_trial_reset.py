"""The once-a-month free-trial re-arm.

A shop's free trial used to be one-per-customer FOR LIFE, so a lapsed customer could never be
won back. A shop admin may now re-arm every customer — but the quota of every trial is excluded
from the reseller's invoice, i.e. paid for by the platform owner, so the rate limit and the
owner's master switch are not cosmetics: they are the entire cost control. These tests pin them,
plus the two things a careless refactor would break — that the announcement rides the SAME
transaction as the reset, and that a replayed idempotency key does neither twice.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models import (
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontBroadcastJob,
    StorefrontCustomer,
    StorefrontDeliveryRecipient,
    StorefrontOrder,
)
from app.services import periods, settings_service, storefront_admin

PERIOD = periods.current_month().label


def _run(body):  # noqa: ANN001, ANN202
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                settings_service.clear_settings_cache()
                await body(session)
        finally:
            settings_service.clear_settings_cache()
            await engine.dispose()

    asyncio.run(go())


def _ctx(version: int = 1, key: str = "reset-1", *, actor: int = 111, source="portal"):  # noqa: ANN001, ANN202
    return storefront_admin.CommandContext(
        actor_telegram_id=actor, actor_role="owner", source=source,
        idempotency_key=key, expected_version=version,
    )


async def _seed(session, *, customers: int = 3, used: int = 2):  # noqa: ANN001, ANN202
    panel = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="owner")
    session.add(panel)
    await session.flush()
    owner = Reseller(panel_id=panel.id, admin_uuid="r1", name="Owner", bot_chat_id=111,
                     storefront_enabled=True)
    session.add(owner)
    await session.flush()
    shop = StorefrontBot(
        reseller_id=owner.id, panel_id=panel.id, bot_token_enc="tok", bot_username="shop_bot",
        config_version=1, free_trial_enabled=True, free_trial_gb=1, free_trial_days=1,
    )
    session.add(shop)
    await session.flush()
    made = []
    for i in range(customers):
        c = StorefrontCustomer(
            storefront_bot_id=shop.id, telegram_id=1000 + i, name=f"C{i}",
            free_trial_used=(i < used),
        )
        session.add(c)
        made.append(c)
    await session.commit()
    return shop, made


async def _used_count(session, shop_id: int) -> int:  # noqa: ANN001
    return int((await session.execute(
        select(func.count()).select_from(StorefrontCustomer).where(
            StorefrontCustomer.storefront_bot_id == shop_id,
            StorefrontCustomer.free_trial_used.is_(True),
        )
    )).scalar_one())


# ---------------------------------------------------------------- the happy path
def test_reset_rearms_every_used_customer_and_announces_to_all(tmp_path):
    async def body(session):
        shop, customers = await _seed(session, customers=3, used=2)
        result = await storefront_admin.reset_free_trials(session, shop.id, _ctx())

        assert result.body["reset_count"] == 2
        assert result.body["period"] == PERIOD
        assert await _used_count(session, shop.id) == 0

        # Exactly ONE announcement, to the whole shop — not just the re-armed customers.
        jobs = (await session.execute(select(StorefrontBroadcastJob))).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].kind == "broadcast" and jobs[0].segment == "all"
        assert jobs[0].total_count == 3 and result.body["notified"] == 3
        recipients = (await session.execute(select(StorefrontDeliveryRecipient))).scalars().all()
        assert {r.customer_id for r in recipients} == {c.id for c in customers}
        # The default template is rendered, not left with raw placeholders.
        assert "{" not in jobs[0].message_text and jobs[0].message_text.strip()

        await session.refresh(shop)
        assert shop.trial_reset_period == PERIOD
        assert shop.config_version == 2          # settings-level change bumps the CAS token
    _run(body)


def test_a_customer_with_a_live_trial_is_reset_too(tmp_path):
    """The owner chose "reset everyone", knowing a still-running trial means the customer can
    hold two free configs at once. Pinned so nobody quietly "fixes" it into a filter."""
    async def body(session):
        shop, customers = await _seed(session, customers=2, used=2)
        session.add(StorefrontOrder(
            customer_id=customers[0].id, panel_id=shop.panel_id, is_trial=True,
            status="provisioned", gb=1, days=1, price_toman=0, panel_user_uuid="live-uuid"))
        await session.commit()

        result = await storefront_admin.reset_free_trials(session, shop.id, _ctx())
        assert result.body["reset_count"] == 2
        assert await _used_count(session, shop.id) == 0
    _run(body)


# ---------------------------------------------------------------- the rate limit
def test_a_second_reset_in_the_same_month_is_refused(tmp_path):
    async def body(session):
        shop, _c = await _seed(session)
        await storefront_admin.reset_free_trials(session, shop.id, _ctx(1, "first"))

        with pytest.raises(storefront_admin.AdminCommandError) as exc:
            await storefront_admin.reset_free_trials(session, shop.id, _ctx(2, "second"))
        assert exc.value.code == "already_reset_this_month"
        assert exc.value.response_status == 409
        # Refused before anything was written: still one job, no re-arm.
        assert (await session.execute(
            select(func.count()).select_from(StorefrontBroadcastJob))).scalar_one() == 1
    _run(body)


def test_the_next_month_is_allowed_again(tmp_path):
    async def body(session):
        shop, _c = await _seed(session, customers=2, used=2)
        await storefront_admin.reset_free_trials(session, shop.id, _ctx(1, "first"))
        # Roll the stamp back a month, exactly as the calendar would.
        shop.trial_reset_period = "1999-01"
        (await session.get(StorefrontCustomer, 1)).free_trial_used = True
        await session.commit()

        result = await storefront_admin.reset_free_trials(session, shop.id, _ctx(2, "next-month"))
        assert result.body["reset_count"] == 1
        await session.refresh(shop)
        assert shop.trial_reset_period == PERIOD
    _run(body)


def test_status_reports_why_the_button_is_unavailable(tmp_path):
    async def body(session):
        shop, _c = await _seed(session, customers=4, used=3)
        status = await storefront_admin.trial_reset_status(session, shop)
        assert status["available"] is True and status["reason"] is None
        assert status["eligible_count"] == 3 and status["last_reset_period"] is None
        assert status["max_gb"] == 1               # the shipped owner default

        await storefront_admin.reset_free_trials(session, shop.id, _ctx())
        await session.refresh(shop)
        status = await storefront_admin.trial_reset_status(session, shop)
        assert status["available"] is False
        assert status["reason"] == "already_reset_this_month"
        assert status["eligible_count"] == 0
    _run(body)


# ---------------------------------------------------------------- the owner's switch
def test_the_owner_master_switch_disables_the_whole_feature(tmp_path):
    async def body(session):
        shop, _c = await _seed(session)
        await settings_service.set_value(session, "storefront_trial_reset_enabled", False)

        with pytest.raises(storefront_admin.AdminCommandError) as exc:
            await storefront_admin.reset_free_trials(session, shop.id, _ctx())
        assert exc.value.code == "trial_reset_disabled"
        assert await _used_count(session, shop.id) == 2
        assert (await storefront_admin.trial_reset_status(session, shop))["reason"] == "disabled"
    _run(body)


# ---------------------------------------------------------------- idempotency & isolation
def test_a_replayed_key_neither_resets_twice_nor_announces_twice(tmp_path):
    """The whole point of routing this through the shared command layer: a retried request must
    return the cached result without touching customers or queueing a second fan-out."""
    async def body(session):
        shop, _c = await _seed(session, customers=3, used=2)
        first = await storefront_admin.reset_free_trials(session, shop.id, _ctx(1, "same-key"))
        # Re-arm one customer so a genuine second run would be visible in reset_count.
        (await session.get(StorefrontCustomer, 3)).free_trial_used = True
        await session.commit()

        replay = await storefront_admin.reset_free_trials(session, shop.id, _ctx(1, "same-key"))
        assert replay.body["job_id"] == first.body["job_id"]
        assert replay.body["reset_count"] == first.body["reset_count"]
        assert (await session.execute(
            select(func.count()).select_from(StorefrontBroadcastJob))).scalar_one() == 1
        # The customer we re-armed by hand is untouched — mutate() never ran again.
        assert (await session.get(StorefrontCustomer, 3)).free_trial_used is True
    _run(body)


def test_a_stale_config_version_loses_the_cas(tmp_path):
    async def body(session):
        shop, _c = await _seed(session)
        shop.config_version = 5
        await session.commit()

        with pytest.raises(storefront_admin.AdminCommandError) as exc:
            await storefront_admin.reset_free_trials(session, shop.id, _ctx(1, "stale"))
        assert exc.value.code == "config_conflict"
        assert await _used_count(session, shop.id) == 2      # nothing re-armed
    _run(body)


def test_resetting_one_shop_leaves_another_shops_customers_alone(tmp_path):
    async def body(session):
        shop_a, _a = await _seed(session, customers=2, used=2)
        other = Reseller(panel_id=shop_a.panel_id, admin_uuid="r2", name="Other",
                         bot_chat_id=222, storefront_enabled=True)
        session.add(other)
        await session.flush()
        shop_b = StorefrontBot(
            reseller_id=other.id, panel_id=shop_a.panel_id, bot_token_enc="tok2",
            bot_username="other_bot", config_version=1)
        session.add(shop_b)
        await session.flush()
        session.add(StorefrontCustomer(
            storefront_bot_id=shop_b.id, telegram_id=9001, name="Foreign", free_trial_used=True))
        await session.commit()

        await storefront_admin.reset_free_trials(session, shop_a.id, _ctx())
        assert await _used_count(session, shop_a.id) == 0
        assert await _used_count(session, shop_b.id) == 1
        # …and the announcement was scoped to shop A.
        jobs = (await session.execute(select(StorefrontBroadcastJob))).scalars().all()
        assert [j.storefront_bot_id for j in jobs] == [shop_a.id]
    _run(body)


def test_an_unusable_template_falls_back_instead_of_failing_the_reset(tmp_path):
    """A shop's customers getting a raw placeholder is bad; a KeyError rolling back the reset is
    worse. The owner's edit degrades to shipped copy."""
    async def body(session):
        shop, _c = await _seed(session)
        await settings_service.set_value(
            session, "tpl_storefront_trial_reset", "سلام {unknown_placeholder}")

        result = await storefront_admin.reset_free_trials(session, shop.id, _ctx())
        job = (await session.execute(select(StorefrontBroadcastJob))).scalar_one()
        assert "{" not in job.message_text and job.message_text.strip()
        assert result.body["reset_count"] == 2
    _run(body)


def test_the_announcement_names_the_shop_and_its_trial_size(tmp_path):
    async def body(session):
        shop, _c = await _seed(session)
        shop.free_trial_days = 3
        await session.commit()

        await storefront_admin.reset_free_trials(session, shop.id, _ctx())
        job = (await session.execute(select(StorefrontBroadcastJob))).scalar_one()
        assert "@shop_bot" in job.message_text
        assert "1" in job.message_text and "3" in job.message_text
    _run(body)


def test_the_announced_size_is_clamped_to_the_owner_cap(tmp_path):
    """A shop configured above the cap before it existed must not advertise a size the claim path
    will refuse to provision."""
    async def body(session):
        shop, _c = await _seed(session)
        shop.free_trial_gb = 50           # legacy value, above the shipped cap of 1
        await session.commit()

        await storefront_admin.reset_free_trials(session, shop.id, _ctx())
        job = (await session.execute(select(StorefrontBroadcastJob))).scalar_one()
        assert "50" not in job.message_text
    _run(body)


def test_the_reset_stamp_is_a_gregorian_billing_month(tmp_path):
    """The limit is expressed in the same months the invoices use, so the giveaway is countable
    per billing period rather than on a rolling clock."""
    async def body(session):
        shop, _c = await _seed(session)
        await storefront_admin.reset_free_trials(session, shop.id, _ctx())
        await session.refresh(shop)
        assert shop.trial_reset_period == periods.current_month().label
        assert dt.datetime.strptime(shop.trial_reset_period, "%Y-%m")
    _run(body)
