"""Storefront free-trial fix: trials are non-renewable, excluded from reseller billing, and a
one-time cleanup resets over-renewed trial quotas back to 1 GB."""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/sftrial.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import (  # noqa: E402
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
    UsageMeter,
)
from app.services import storefront, storefront_subscription  # noqa: E402
from app.services.invoice_engine import (  # noqa: E402
    billable_gb_for_user,
    compute_invoices,
)
from app.services.periods import month_period  # noqa: E402


def _run(body, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s, Session)
        finally:
            await engine.dispose()

    asyncio.run(go())


# ── billing exclusion (pure engine) ───────────────────────────────────────────

def _reseller(uuid, parent, **kw):
    return SimpleNamespace(admin_uuid=uuid, parent_admin_uuid=parent, is_owner=False,
                           exclude_from_billing=False, price_per_gb=None, name=uuid,
                           id=hash(uuid) & 0xffff, min_sale_toman=None, **kw)


def _user(uuid, added_by, gb):
    return SimpleNamespace(user_uuid=uuid, added_by_uuid=added_by, name=uuid,
                           start_date=dt.date(2026, 2, 10), usage_limit_gb=gb)


def test_billable_gb_for_user_skips_excluded_uuid():
    period = month_period(2026, 2)
    u = _user("trial-uuid", "r1", 2)
    assert billable_gb_for_user(u, period, set(), 1.0) is not None          # normally billed (2 GB)
    assert billable_gb_for_user(u, period, set(), 1.0, exclude_user_uuids={"trial-uuid"}) is None


def test_compute_invoices_excludes_trial_config():
    owner = SimpleNamespace(admin_uuid="owner", parent_admin_uuid=None, is_owner=True,
                            exclude_from_billing=False, price_per_gb=None, name="o",
                            id=1, min_sale_toman=None)
    r1 = _reseller("r1", "owner")
    users = [_user("u-normal", "r1", 2), _user("u-trial", "r1", 2)]  # both 2 GB (renewed trial)
    bundles = compute_invoices(
        [owner, r1], users, month_period(2026, 2),
        default_price_per_gb=1000, excluded_usage_gb=set(), free_threshold_gb=1.0,
        exclude_user_uuids={"u-trial"},
    )
    assert len(bundles) == 1
    assert round(bundles[0].total_gb, 3) == 2.0   # only the non-trial 2 GB; the trial is free


# ── metering exclusion + helper + renewal block + cleanup (DB) ────────────────

async def _seed(s, *, gb=2, is_trial=True, status="provisioned"):
    panel = Panel(key="p", host="h", proxy_path_enc="x", owner_uuid="o")
    s.add(panel)
    await s.flush()
    r = Reseller(panel_id=panel.id, admin_uuid="a", name="R")
    s.add(r)
    await s.flush()
    bot = StorefrontBot(reseller_id=r.id, panel_id=panel.id,
                        bot_token_enc=crypto.encrypt("111:tok") or "", enabled=True, status="active")
    s.add(bot)
    await s.flush()
    cust = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=5)
    s.add(cust)
    await s.flush()
    order = StorefrontOrder(customer_id=cust.id, panel_id=panel.id, plan_id=None,
                            gb=gb, days=1, price_toman=0, status=status,
                            panel_user_uuid="uu-1", is_trial=is_trial)
    s.add(order)
    await s.commit()
    return panel, r, order


def test_trial_user_uuids_helper(tmp_path):
    async def body(s, _S):
        panel, r, order = await _seed(s, is_trial=True)
        # a paid order alongside → must NOT be returned
        s.add(StorefrontOrder(customer_id=order.customer_id, panel_id=panel.id, plan_id=1,
                              gb=50, days=30, price_toman=100000, status="provisioned",
                              panel_user_uuid="paid-uu", is_trial=False))
        await s.commit()
        uuids = await storefront.trial_user_uuids(s, panel.id)
        assert uuids == {"uu-1"}
    _run(body, tmp_path, "t1.db")


def test_bundle_extra_excludes_trial_meter(tmp_path):
    from app.services import metering

    async def body(s, _S):
        panel, r, order = await _seed(s)
        await __import__("app.services.settings_service", fromlist=["set_value"]).set_value(
            s, "metering_enabled", True)
        s.add(UsageMeter(panel_id=panel.id, user_uuid="uu-1", added_by_uuid="a",
                         period_label="2026-06", edit_renewal_gb=2.0, overage_gb=0.0))
        await s.commit()
        billed = await metering.bundle_extra(s, panel.id, {"a"}, "2026-06", 1.0)
        assert billed["gb"] > 0                                  # normally the renew-by-edit bills
        excl = await metering.bundle_extra(s, panel.id, {"a"}, "2026-06", 1.0,
                                           exclude_user_uuids={"uu-1"})
        assert excl["gb"] == 0.0                                 # trial excluded → nothing billed
    _run(body, tmp_path, "t2.db")


def test_renew_rejects_trial(tmp_path, monkeypatch):
    async def body(s, Session):
        _panel, _r, order = await _seed(s, is_trial=True)

        async def _boom(*a, **k):
            raise AssertionError("renew_user must NOT be called for a trial")

        monkeypatch.setattr(
            "app.services.panel_client.admin_api.AdminApiClient.renew_user", _boom)
        res = await storefront_subscription.renew(Session, order_id=order.id, by_admin=False)
        assert res.ok is False and res.reason == "trial"
    _run(body, tmp_path, "t3.db")


def test_reset_over_renewed_trials(tmp_path, monkeypatch):
    from app.services import storefront_provision
    from app.services.storefront_provision import LiveStatus

    async def body(s, _S):
        from app.models import EndUserSnapshot

        panel, r, order = await _seed(s, is_trial=True)  # trial, order.gb stays 1 (realistic)
        # An over-renewed trial is identified by the SNAPSHOT quota (2 GB), not order.gb.
        s.add(EndUserSnapshot(panel_id=panel.id, user_uuid="uu-1", added_by_uuid="a",
                              usage_limit_gb=2.0, enable=True))
        # A second trial whose snapshot is already 1 GB → must NOT be selected.
        s.add(StorefrontOrder(customer_id=order.customer_id, panel_id=panel.id, plan_id=None,
                              gb=1, days=1, price_toman=0, status="provisioned",
                              panel_user_uuid="ok-uu", is_trial=True))
        s.add(EndUserSnapshot(panel_id=panel.id, user_uuid="ok-uu", added_by_uuid="a",
                              usage_limit_gb=1.0, enable=True))
        await s.commit()

        calls: list[tuple] = []

        async def fake_live(_session, _sf, _order):
            return LiveStatus(True, used_gb=0.5, limit_gb=2.0)

        async def fake_patch(_self, _panel, uuid, body_dict, *, api_key=None):
            calls.append((uuid, body_dict, api_key))

        monkeypatch.setattr(storefront_provision, "live_status", fake_live)
        monkeypatch.setattr(
            "app.services.panel_client.admin_api.AdminApiClient.patch_user", fake_patch)

        counts = await storefront_subscription.reset_over_renewed_trials(s)
        assert counts["reset"] == 1 and counts["checked"] == 1   # only the 2 GB-snapshot trial
        assert calls == [("uu-1", {"usage_limit_GB": 1.0}, "a")]
    _run(body, tmp_path, "t4.db")


# ── the owner's GB ceiling ────────────────────────────────────────────────────
# A trial's quota is excluded from the reseller's invoice at ANY size (see the exclusion tests
# above), so `storefront_trial_max_gb` is the only thing bounding what the platform owner gives
# away. It is enforced twice on purpose: on the write path, and again when a config is actually
# provisioned — so a shop configured above the cap BEFORE the cap existed stops costing more
# without needing a remediation sweep over every shop.

def test_update_trial_refuses_a_size_above_the_owner_cap(tmp_path):
    from app.services import settings_service, storefront_admin

    async def body(s, _S):
        panel = Panel(key="p1", host="h", proxy_path_enc="x", owner_uuid="o")
        s.add(panel)
        await s.flush()
        r = Reseller(panel_id=panel.id, admin_uuid="a", name="R", bot_chat_id=111,
                     storefront_enabled=True)
        s.add(r)
        await s.flush()
        sf = StorefrontBot(reseller_id=r.id, panel_id=panel.id, bot_token_enc="t",
                           bot_username="b", config_version=1)
        s.add(sf)
        await s.commit()
        ctx = storefront_admin.CommandContext(
            actor_telegram_id=111, actor_role="owner", source="portal",
            idempotency_key="k1", expected_version=1)

        try:
            await storefront_admin.update_trial(s, sf.id, ctx, gb=2)
        except storefront_admin.AdminCommandError as exc:
            assert exc.code == "validation"
            assert "۱" in str(exc) or "1" in str(exc)     # the message teaches the ceiling
        else:
            raise AssertionError("a 2 GB trial must be refused under the default 1 GB cap")

        # Raising the owner's cap makes the same edit legal — the bound is a setting, not a constant.
        await settings_service.set_value(s, "storefront_trial_max_gb", 5)
        settings_service.clear_settings_cache()
        ctx2 = storefront_admin.CommandContext(
            actor_telegram_id=111, actor_role="owner", source="portal",
            idempotency_key="k2", expected_version=1)
        await storefront_admin.update_trial(s, sf.id, ctx2, gb=5)
        await s.refresh(sf)
        assert sf.free_trial_gb == 5
        settings_service.clear_settings_cache()
    _run(body, tmp_path, "t5.db")


def test_claim_trial_clamps_a_legacy_over_cap_shop(tmp_path):
    """A shop whose `free_trial_gb` predates the cap must provision the CAPPED size, not its own."""
    from app.services import storefront_provision

    async def body(s, Session):
        panel = Panel(key="p1", host="h", proxy_path_enc="x", owner_uuid="o")
        s.add(panel)
        await s.flush()
        r = Reseller(panel_id=panel.id, admin_uuid="a", name="R", storefront_enabled=True)
        s.add(r)
        await s.flush()
        sf = StorefrontBot(reseller_id=r.id, panel_id=panel.id, bot_token_enc="t",
                           bot_username="b", free_trial_enabled=True, free_trial_gb=50,
                           free_trial_days=1)
        s.add(sf)
        await s.flush()
        cust = StorefrontCustomer(storefront_bot_id=sf.id, telegram_id=7, name="C")
        s.add(cust)
        await s.commit()
        sf_id, cust_id = sf.id, cust.id

        seen: dict = {}

        async def fake_provision(_session, _bot, _customer, *, gb, days, label=None,
                                 user_uuid=None):
            seen["gb"], seen["days"] = gb, days
            return storefront_provision.ProvisionResult(
                True, sub_link="https://x/sub", uuid=user_uuid)

        original = storefront_provision.provision
        storefront_provision.provision = fake_provision
        try:
            res = await storefront_provision.claim_trial(
                Session, sf_id=sf_id, customer_id=cust_id)
        finally:
            storefront_provision.provision = original

        assert res.ok is True
        assert seen["gb"] == 1        # clamped from the shop's stale 50, not honoured
        async with Session() as s2:
            order = (await s2.execute(
                __import__("sqlalchemy").select(StorefrontOrder))).scalars().one()
            assert order.gb == 1 and order.is_trial is True
    _run(body, tmp_path, "t6.db")
