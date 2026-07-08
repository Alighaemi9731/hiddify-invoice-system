"""H09 — billing engine hardening + rates.

- orphaned/cyclic subtrees are reported in GenerationSummary.unbilled_subtrees, not silently
  dropped;
- a deleted free-trial config still stays excluded from the reseller's invoice;
- TON/AVAX auto rates fall back to manual when the cached rate is stale;
- rate_max_age of 0 disables the staleness check;
- pdf.gb renders one decimal for fractional values.
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/h09.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import EndUserSnapshot, Panel, Reseller  # noqa: E402
from app.services import invoicing, pdf, rates, settings_service  # noqa: E402
from app.services.periods import Period  # noqa: E402

PERIOD = Period(dt.date(2026, 6, 1), dt.date(2026, 6, 30))


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await coro_fn(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


def test_pdf_gb_one_decimal_for_fractional():
    assert pdf.gb(4) == pdf._fa_digits("4")
    assert pdf.gb(1.4) == pdf._fa_digits("1.4")
    assert pdf.gb(2.0) == pdf._fa_digits("2")
    assert pdf.gb(1234.5) == pdf._fa_digits("1,234.5")


def test_orphan_subtree_reported_not_silently_unbilled(tmp_path):
    async def body(s):
        now = dt.datetime.now(dt.timezone.utc)
        p = Panel(id=1, key="p1", name="p1", host="h", proxy_path_enc=crypto.encrypt("x"),
                  owner_uuid="owner", last_synced_at=now)
        s.add(p)
        await s.flush()
        # Owner + a billable root + an ORPHAN (its parent 'ghost' doesn't exist).
        s.add(Reseller(panel_id=1, admin_uuid="owner", name="O", is_owner=True, last_seen_at=now))
        s.add(Reseller(panel_id=1, admin_uuid="root", name="Root", parent_admin_uuid="owner",
                       last_seen_at=now))
        s.add(Reseller(panel_id=1, admin_uuid="orphan", name="Orphan",
                       parent_admin_uuid="ghost", last_seen_at=now))
        s.add(EndUserSnapshot(panel_id=1, user_uuid="u1", name="u1", added_by_uuid="orphan",
                              usage_limit_gb=10, start_date=dt.date(2026, 6, 5), enable=True,
                              last_synced_at=now))
        await s.commit()

        summary = await invoicing.generate_invoices(s, PERIOD, panel_id=1)
        assert any("orphan" in u for u in summary.unbilled_subtrees), summary.unbilled_subtrees

    _run(body, tmp_path, "o1.db")


def test_ton_rate_falls_back_to_manual_when_stale(tmp_path):
    async def body(s):
        await settings_service.set_value(s, "ton_rate_mode", "auto")
        await settings_service.set_value(s, "ton_toman_manual", 100_000)
        await settings_service.set_value(s, "ton_toman_auto", 250_000)
        await settings_service.set_value(s, "rate_max_age_hours", 48)
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=100)).isoformat(
            timespec="seconds")
        await settings_service.set_value(s, "ton_toman_auto_at", old)
        # Stale cached auto → manual.
        assert await rates.get_ton_toman(s) == 100_000
        # Fresh → auto.
        fresh = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        await settings_service.set_value(s, "ton_toman_auto_at", fresh)
        assert await rates.get_ton_toman(s) == 250_000
        # max_age 0 disables the check → auto even though stamp is old.
        await settings_service.set_value(s, "ton_toman_auto_at", old)
        await settings_service.set_value(s, "rate_max_age_hours", 0)
        assert await rates.get_ton_toman(s) == 250_000

    _run(body, tmp_path, "t1.db")
