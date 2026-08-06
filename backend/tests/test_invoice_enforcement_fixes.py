"""Two fixes:
1. The interim/sub invoice PDF now includes the abuse-metered extra, so the PDF total matches the
   interim TEXT (`interim_breakdown`) and the real end-of-month invoice.
2. The enforcement queue processes one lane PER PANEL, so actions on different panels are handled in
   the same tick (previously a single global action per tick).
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/fixes.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core.db import Base  # noqa: E402
from app.models import EndUserSnapshot, Panel, Reseller  # noqa: E402
from app.models.enums import EnforcementState  # noqa: E402
from app.services import metering, reseller_report  # noqa: E402
from app.services.periods import month_period  # noqa: E402
from tests.panel_fakes import as_identity  # noqa: E402


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await coro_fn(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


# ───────────────────────── Fix 1: interim PDF == interim text ─────────────────────────
def test_interim_pdf_lines_match_text_with_metering(tmp_path, monkeypatch):
    period = month_period(2026, 5)

    async def fake_bundle_extra(session, panel_id, uuids, period_label, free_threshold,
                                exclude_user_uuids=None):
        # A 5 GB abuse extra on one of the reseller's users (typed-line shape).
        return {"gb": 5.0, "abnormal": [],
                "lines": [{"user_uuid": "x1", "name": "ابر مصرف",
                           "display_name": "ابر مصرف — مصرف مازاد بر بسته",
                           "usage_gb": 5.0, "added_by_uuid": "A", "kind": "overage"}]}

    monkeypatch.setattr(metering, "bundle_extra", fake_bundle_extra)

    async def body(s):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        s.add(Reseller(panel_id=1, admin_uuid="A", name="R", price_per_gb=1000))
        # One billable own user (created in the period, 10 GB > free threshold).
        s.add(EndUserSnapshot(panel_id=1, user_uuid="u1", name="cust", added_by_uuid="A",
                              usage_limit_gb=10, start_date=dt.date(2026, 5, 10),
                              enable=True, is_active=True))
        await s.commit()
        r = (await s.execute(
            select(Reseller).where(Reseller.admin_uuid == "A")
        )).scalar_one()

        bd = await reseller_report.interim_breakdown(s, r, period)
        result = await reseller_report.node_invoice_pdf_lines(s, r, period, own_only=True)
        assert result is not None
        lines, total_gb = result

        # Base 10 + metering extra 5 = 15, identical to the text breakdown's own GB.
        assert total_gb == pytest.approx(15.0)
        assert bd["own"]["gb"] == pytest.approx(15.0)
        assert total_gb == pytest.approx(bd["own"]["gb"])
        # The metered extra appears as its own TYPED labelled line in the PDF.
        assert any((ln["name"] or "").endswith("مصرف مازاد بر بسته") for ln in lines)
        assert sum(ln["usage_gb"] for ln in lines) == pytest.approx(15.0)

    _run(body, tmp_path, "fixes.db")


# ───────────────────────── Fix 2: per-panel parallel enforcement ─────────────────────────
def test_enforcement_processes_multiple_panels_in_one_tick(tmp_path, monkeypatch):
    from app.services import enforcement, settings_service

    async def body(s):
        await settings_service.set_value(s, "enforcement_enabled", True)
        # Keep panel concurrency at 1 for SQLite stability — still proves both panels are
        # handled in ONE call (the old worker processed a single global action per tick).
        await settings_service.set_value(s, "enforcement_panel_concurrency", 1)
        for pid, uuid in ((1, "A"), (2, "B")):
            s.add(Panel(id=pid, key=f"p{pid}", host=f"h{pid}", proxy_path_enc="x", owner_uuid="o"))
            s.add(Reseller(panel_id=pid, admin_uuid=uuid, name=f"R{pid}", panel_max_users=10,
                           panel_max_active_users=10, enforcement_state=EnforcementState.active))
            s.add(EndUserSnapshot(panel_id=pid, user_uuid=f"u{pid}", name="c",
                                  added_by_uuid=uuid, enable=True))
        await s.commit()
        rows = (await s.execute(select(Reseller))).scalars().all()
        for r in rows:
            await enforcement.queue_enforcement(s, r, dry_run=False)

        async def fake_user_id(self, panel, user_uuid, *, api_key=None):
            return 100 + panel.id if user_uuid == f"u{panel.id}" else None

        async def fake_bulk(self, panel, user_ids, enabled, *, api_key=None):
            return None

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            return (10, 10)

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            return None

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_identity", as_identity(fake_user_id))
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        agg = await enforcement.process_enforcement_queue(s)
        # BOTH panels processed in a single tick, both fully suspended.
        assert agg["panels"] == 2
        assert agg["done"] == 2
        for r in rows:
            await s.refresh(r)
            assert r.enforcement_state == EnforcementState.enforced

    _run(body, tmp_path, "fixes.db")


def test_enforcement_panel_concurrency_setting_range():
    from app.services import settings_service
    assert settings_service.validate_api_value("enforcement_panel_concurrency", 6) == 6
    for bad in (0, 21):
        with pytest.raises(ValueError):
            settings_service.validate_api_value("enforcement_panel_concurrency", bad)
