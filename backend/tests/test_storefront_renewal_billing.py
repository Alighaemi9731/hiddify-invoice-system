"""A renewed storefront config must be billed the plan it SOLD, not its inflated panel quota.

Customer-bot renewals set the panel config's `usage_limit_GB = current_usage + plan_gb` (cumulative),
so the quota inflates every cycle. The reseller (admin) invoice reads that quota, so a renewed config
double-counts the previous cycle's consumption — verified on production as ~2× over-billing that
compounds monthly.

The fix bills each storefront-managed config at `StorefrontOrder.gb` (the sold plan), via a per-uuid
CAP threaded through the one billing decision point (`invoice_engine.billable_gb_for_user`) and used
by BOTH the real invoice and the «فاکتور علی‌الحساب» interim. These tests pin that: the inflated
config bills the plan, a native (non-shop) config is untouched, a trial stays excluded, and the cap
never RAISES a bill.
"""
from __future__ import annotations

import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/sfrenewbill.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import (  # noqa: E402
    EndUserSnapshot,
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
)
from app.services import reseller_report, storefront  # noqa: E402
from app.services.invoice_engine import billable_gb_for_user  # noqa: E402
from app.services.periods import Period, current_month  # noqa: E402


def _run(body, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()

    asyncio.run(go())


def _snap(panel_id, uuid, added_by, limit, used, start):
    return EndUserSnapshot(
        panel_id=panel_id, user_uuid=uuid, name="گوشی", added_by_uuid=added_by,
        usage_limit_gb=limit, current_usage_gb=used, enable=True, is_active=True,
        meter_provisioned_gb=0, meter_consumed_gb=0, meter_init=False, start_date=start,
    )


async def _seed_shop(s):
    """A reseller with a storefront bot + one customer; returns (reseller, bot, customer)."""
    p = Panel(id=1, key="p1", host="h", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o",
              status="ok")
    s.add(p)
    await s.flush()
    r = Reseller(panel_id=1, admin_uuid="A", name="R", price_per_gb=1000, storefront_enabled=True)
    s.add(r)
    await s.flush()
    bot = StorefrontBot(reseller_id=r.id, panel_id=1, bot_token_enc=crypto.encrypt("t"),
                        bot_username="shop", enabled=True, status="active")
    s.add(bot)
    await s.flush()
    cust = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=1, name="C")
    s.add(cust)
    await s.flush()
    return r, bot, cust


async def _order(s, cust, uuid, gb, *, is_trial=False):
    o = StorefrontOrder(customer_id=cust.id, panel_id=1, label="گوشی", gb=gb, days=30,
                        price_toman=gb * 1000, status="provisioned", panel_user_uuid=uuid,
                        is_trial=is_trial)
    s.add(o)
    await s.flush()
    return o


PERIOD: Period = current_month()
START = PERIOD.start


def test_billing_caps_maps_uuid_to_the_sold_plan(tmp_path):
    async def body(s):
        _r, _bot, cust = await _seed_shop(s)
        await _order(s, cust, "u-inflated", 50)
        await _order(s, cust, "u-trial", 1, is_trial=True)
        await s.commit()

        caps = await storefront.billing_caps(s, 1)
        assert caps == {"u-inflated": 50.0}, caps      # trial omitted (never billed anyway)

    _run(body, tmp_path, "caps.db")


# The per-user rule is a pure function — no DB needed.
def test_inflated_config_bills_the_plan_not_the_panel_quota():
    """The core fix: a config whose panel quota is 91.5 (used 41.5 + plan 50) bills 50."""
    excluded: set[float] = set()
    snap = _snap(1, "u1", "A", limit=91.531, used=42.393, start=START)   # in-period start

    raw = billable_gb_for_user(snap, PERIOD, excluded, 1.0)              # no cap → the bug
    assert raw is not None and abs(raw[0] - 91.531) < 1e-6

    capped = billable_gb_for_user(snap, PERIOD, excluded, 1.0, cap_gb_by_uuid={"u1": 50.0})
    assert capped is not None and abs(capped[0] - 50.0) < 1e-6


def test_cap_never_raises_a_bill():
    """A fresh config whose quota is BELOW its plan (partial provision) must not be billed UP."""
    excluded: set[float] = set()
    snap = _snap(1, "u1", "A", limit=30.0, used=0, start=START)
    capped = billable_gb_for_user(snap, PERIOD, excluded, 1.0, cap_gb_by_uuid={"u1": 50.0})
    assert capped is not None and abs(capped[0] - 30.0) < 1e-6          # min(30, 50) = 30


def test_native_config_is_untouched():
    """A config NOT in the storefront map (created directly in Hiddify) bills its real quota."""
    excluded: set[float] = set()
    snap = _snap(1, "native", "A", limit=200.0, used=10, start=START)
    capped = billable_gb_for_user(snap, PERIOD, excluded, 1.0, cap_gb_by_uuid={"u1": 50.0})
    assert capped is not None and abs(capped[0] - 200.0) < 1e-6


def test_interim_bills_the_plan_for_a_renewed_shop_config(tmp_path):
    """End-to-end through the «فاکتور علی‌الحساب» path: an inflated shop config shows the plan."""
    async def body(s):
        r, _bot, cust = await _seed_shop(s)
        await _order(s, cust, "u1", 50)
        s.add(_snap(1, "u1", "A", limit=91.531, used=42.393, start=START))
        await s.commit()

        out = await reseller_report.interim_breakdown(s, r, current_month())
        # own = the reseller's own config; billed the sold plan (50), not 91.5.
        assert abs(out["own"]["gb"] - 50.0) < 1e-6, out["own"]
        assert abs(out["total_gb"] - 50.0) < 1e-6
        assert out["total_amount"] == 50_000            # 50 GB × 1000 T

    _run(body, tmp_path, "interim.db")


def test_interim_without_the_cap_would_show_the_inflated_number(tmp_path):
    """Guard the guard: prove the config really IS inflated, so the previous test isn't vacuous —
    a NATIVE config with the same snapshot (no matching order) shows the full 91.5."""
    async def body(s):
        r, _bot, _cust = await _seed_shop(s)
        # No storefront order for this uuid → not in the caps map → billed verbatim.
        s.add(_snap(1, "orphan", "A", limit=91.531, used=42.393, start=START))
        await s.commit()

        out = await reseller_report.interim_breakdown(s, r, current_month())
        assert abs(out["own"]["gb"] - 91.531) < 0.01, out["own"]   # interim rounds to 2 dp

    _run(body, tmp_path, "interim_raw.db")
