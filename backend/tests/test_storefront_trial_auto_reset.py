"""The scheduled fleet-wide free-trial re-arm (`storefront_trial_reset.sweep`).

The per-shop button was removed because shop admins almost never pressed it, so this sweep is now
the ONLY thing that ever re-arms a trial. Two consequences drive every test here:

* it runs unattended across ~150 shops, so eligibility must be exact — a shop whose bot is dead,
  whose trial is off, or which is closed cannot be told «تستت برگشت», and one bad shop must not
  stop the other 149;
* every re-armed customer who claims costs the PLATFORM, not the reseller (a trial's quota is
  excluded from the reseller's invoice), so "runs at most once a month" and "never announces
  twice" are cost controls, not tidiness.

The reset command itself is covered by `test_storefront_trial_reset.py`; this file is about the
loop around it.
"""
from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models import (
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontBroadcastJob,
    StorefrontCustomer,
)
from app.models.setting import Setting
from app.services import periods, settings_service, storefront_trial_reset

PERIOD = periods.current_month().label


def _run(monkeypatch, body):  # noqa: ANN001, ANN202
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        settings_service.clear_settings_cache()
        monkeypatch.setattr(storefront_trial_reset, "SessionLocal", factory)
        # The sweep's own summary DM is not what these tests are about, and it would try to build
        # a real Bot from an unset owner chat.
        monkeypatch.setattr(storefront_trial_reset, "_notify_owner", _noop)
        try:
            await body(factory)
        finally:
            settings_service.clear_settings_cache()
            await engine.dispose()

    asyncio.run(go())


async def _noop(_counts):  # noqa: ANN001, ANN202
    return None


async def _seed(  # noqa: ANN202
    factory, shops,  # noqa: ANN001
):
    """`shops` is a list of kwargs overrides; each gets 2 customers, both with a used trial.

    Shops are backdated by default: a shop created inside the CURRENT period is deliberately
    skipped by the sweep, so a fixture built "now" would make every test silently trivial. Pass
    `created_at=` to opt back in."""
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=200)
    ids = {}
    async with factory() as s:
        panel = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="owner")
        s.add(panel)
        await s.flush()
        for i, overrides in enumerate(shops):
            name = overrides.pop("name", f"shop{i}")
            reseller = Reseller(panel_id=panel.id, admin_uuid=f"a{i}", name=name,
                                bot_chat_id=1000 + i, storefront_enabled=True)
            s.add(reseller)
            await s.flush()
            defaults = dict(
                reseller_id=reseller.id, panel_id=panel.id, bot_token_enc="t",
                bot_username=name, config_version=1, enabled=True, status="active",
                free_trial_enabled=True, free_trial_gb=1, free_trial_days=1,
                created_at=old, updated_at=old,
            )
            defaults.update(overrides)
            shop = StorefrontBot(**defaults)
            s.add(shop)
            await s.flush()
            s.add_all([
                StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=9000 + i * 10 + n,
                                   name=f"{name}-c{n}", free_trial_used=True)
                for n in range(2)
            ])
            ids[name] = shop.id
        s.add(Setting(key="storefront_trial_max_gb", value=1))
        await s.commit()
    settings_service.clear_settings_cache()
    return ids


async def _used(factory, shop_id: int) -> int:
    async with factory() as s:
        return int((await s.execute(
            select(func.count()).select_from(StorefrontCustomer).where(
                StorefrontCustomer.storefront_bot_id == shop_id,
                StorefrontCustomer.free_trial_used.is_(True)))).scalar_one() or 0)


async def _jobs(factory) -> list[StorefrontBroadcastJob]:
    async with factory() as s:
        return list((await s.execute(select(StorefrontBroadcastJob))).scalars().all())


def test_sweep_rearms_every_eligible_shop_and_announces_once(monkeypatch):
    async def body(factory):  # noqa: ANN001
        ids = await _seed(factory, [{"name": "a"}, {"name": "b"}])
        result = await storefront_trial_reset.sweep()

        assert result["shops"] == 2
        assert result["customers"] == 4          # 2 shops × 2 used trials
        assert result["notified"] == 4
        assert result["period"] == PERIOD
        for shop_id in ids.values():
            assert await _used(factory, shop_id) == 0
        jobs = await _jobs(factory)
        assert len(jobs) == 2
        assert {j.kind for j in jobs} == {"trial_reset"}
        async with factory() as s:
            for shop_id in ids.values():
                shop = await s.get(StorefrontBot, shop_id)
                assert shop.trial_reset_period == PERIOD

    _run(monkeypatch, body)


def test_sweep_skips_shops_it_must_never_message(monkeypatch):
    """Eligibility, one shop per reason. Each of these would produce a lie, a dead send, or both.

    `status` is the liveness axis, not `enabled`: nothing in the backend ever clears `enabled`, so
    a sweep gated on it would keep announcing through revoked and errored tokens forever."""
    async def body(factory):  # noqa: ANN001
        ids = await _seed(factory, [
            {"name": "good"},
            {"name": "errored", "status": "errored"},
            {"name": "revoked", "status": "revoked"},
            {"name": "trial_off", "free_trial_enabled": False},
            {"name": "closed", "shop_closed": True},
            {"name": "done", "trial_reset_period": PERIOD},
        ])
        result = await storefront_trial_reset.sweep()

        assert result["shops"] == 1
        assert await _used(factory, ids["good"]) == 0
        for name in ("errored", "revoked", "trial_off", "closed", "done"):
            assert await _used(factory, ids[name]) == 2, f"{name} must be untouched"
        jobs = await _jobs(factory)
        assert len(jobs) == 1

    _run(monkeypatch, body)


def test_a_shop_created_this_month_waits_for_the_next_one(monkeypatch):
    """A brand-new shop has nothing to re-arm, and «تست رایگانت دوباره فعال شد» is nonsense to a
    customer who never had one. It joins the fleet next month."""
    async def body(factory):  # noqa: ANN001
        now = dt.datetime.now(dt.timezone.utc)
        ids = await _seed(
            factory, [{"name": "fresh", "created_at": now, "updated_at": now}, {"name": "old"}])
        result = await storefront_trial_reset.sweep()

        assert result["shops"] == 1
        assert await _used(factory, ids["old"]) == 0
        assert await _used(factory, ids["fresh"]) == 2

    _run(monkeypatch, body)


def test_master_switch_off_writes_nothing(monkeypatch):
    async def body(factory):  # noqa: ANN001
        ids = await _seed(factory, [{"name": "a"}])
        async with factory() as s:
            s.add(Setting(key="storefront_trial_reset_enabled", value=False))
            await s.commit()
        settings_service.clear_settings_cache()

        result = await storefront_trial_reset.sweep()
        assert result["skipped"] == "disabled"
        assert result["shops"] == 0
        assert await _used(factory, ids["a"]) == 2
        assert await _jobs(factory) == []

    _run(monkeypatch, body)


def test_a_second_run_in_the_same_month_neither_rearms_nor_reannounces(monkeypatch):
    """The retry days (the sweep is registered on the reset day PLUS two) must be free.

    Two independent guards have to hold: the stamp filters the shop out before the command is even
    called, and the constant per-period idempotency key would replay rather than repeat if it
    somehow were."""
    async def body(factory):  # noqa: ANN001
        ids = await _seed(factory, [{"name": "a"}])
        first = await storefront_trial_reset.sweep()
        assert first["shops"] == 1

        # Simulate the customers claiming again before the retry day comes round.
        async with factory() as s:
            for c in (await s.execute(select(StorefrontCustomer))).scalars().all():
                c.free_trial_used = True
            await s.commit()

        second = await storefront_trial_reset.sweep()
        assert second["shops"] == 0 and second["customers"] == 0
        assert len(await _jobs(factory)) == 1
        assert await _used(factory, ids["a"]) == 2   # NOT re-armed a second time this month

    _run(monkeypatch, body)


def test_one_failing_shop_does_not_stop_the_fleet(monkeypatch):
    """150 shops per run: a single bad one must be logged and stepped over, never fatal."""
    async def body(factory):  # noqa: ANN001
        ids = await _seed(factory, [{"name": "a"}, {"name": "boom"}, {"name": "c"}])
        from app.services import storefront_admin

        real = storefront_admin.reset_free_trials

        async def flaky(session, shop_id, ctx):  # noqa: ANN001, ANN202
            if shop_id == ids["boom"]:
                raise RuntimeError("panel on fire")
            return await real(session, shop_id, ctx)

        monkeypatch.setattr(storefront_admin, "reset_free_trials", flaky)
        result = await storefront_trial_reset.sweep()

        assert result["shops"] == 2 and result["failed"] == 1
        assert await _used(factory, ids["a"]) == 0
        assert await _used(factory, ids["c"]) == 0
        assert await _used(factory, ids["boom"]) == 2

    _run(monkeypatch, body)


def test_dry_run_reports_without_touching_anything(monkeypatch):
    """`dry_run` is the ops endpoint's default, so it must be provably inert — this is the switch
    that stands between a curious POST and thousands of messages."""
    async def body(factory):  # noqa: ANN001
        ids = await _seed(factory, [{"name": "a"}, {"name": "b"}])
        result = await storefront_trial_reset.sweep(dry_run=True)

        assert result["dry_run"] is True
        assert result["shops"] == 2 and result["pending_shops"] == 2
        assert await _jobs(factory) == []
        for shop_id in ids.values():
            assert await _used(factory, shop_id) == 2
        async with factory() as s:
            for shop_id in ids.values():
                assert (await s.get(StorefrontBot, shop_id)).trial_reset_period is None

    _run(monkeypatch, body)


def test_report_counts_the_giveaway_before_it_happens(monkeypatch):
    async def body(factory):  # noqa: ANN001
        await _seed(factory, [{"name": "a"}, {"name": "off", "free_trial_enabled": False}])
        out = await storefront_trial_reset.report()

        assert out["enabled"] is True
        assert out["period"] == PERIOD
        assert out["active_shops"] == 2      # both bots poll…
        assert out["pending_shops"] == 1     # …but only one would be re-armed
        assert out["shops"] == ["a"]

    _run(monkeypatch, body)


def test_next_reset_date_rolls_into_the_following_month():
    """Pure date helper behind the customer-facing «ماهِ آینده» copy."""
    d = storefront_trial_reset.next_reset_date
    assert d(1, dt.date(2026, 8, 1)) == dt.date(2026, 8, 1)     # today still counts
    assert d(1, dt.date(2026, 8, 18)) == dt.date(2026, 9, 1)
    assert d(28, dt.date(2026, 1, 31)) == dt.date(2026, 2, 28)  # exists in February by construction
    assert d(5, dt.date(2026, 12, 20)) == dt.date(2027, 1, 5)   # year rollover
