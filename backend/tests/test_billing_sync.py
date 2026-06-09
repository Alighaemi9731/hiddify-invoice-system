"""Billing & sync correctness (B04).

Covers: a panel whose latest sync failed (or never ran) is excluded from billing; a reseller
removed from the panel (older last_seen_at than the panel's last_synced_at) is not billed; the
backup parser/fetch refuses a partial backup; and the auto exchange rate falls back to manual
when the cached live rate is stale.
"""
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/bill.db")
os.environ.setdefault("SECRET_KEY", "k")

from types import SimpleNamespace  # noqa: E402

from app.models.enums import PanelStatus  # noqa: E402
from app.services import invoicing, rates  # noqa: E402


# ------------------------------------------------ panel billability
def test_panel_not_billable_when_sync_failed_or_missing():
    now = dt.datetime.now(dt.timezone.utc)
    ok = SimpleNamespace(key="p1", last_synced_at=now, status=PanelStatus.ok)
    failed = SimpleNamespace(key="p2", last_synced_at=now, status=PanelStatus.error)
    never = SimpleNamespace(key="p3", last_synced_at=None, status=PanelStatus.ok)
    assert invoicing._panel_billable(ok)[0] is True
    assert invoicing._panel_billable(failed)[0] is False
    assert invoicing._panel_billable(never)[0] is False


# ------------------------------------------------ reseller presence
def test_removed_reseller_not_billed():
    synced = dt.datetime(2026, 2, 1, 9, 0, tzinfo=dt.timezone.utc)
    panel = SimpleNamespace(last_synced_at=synced)
    present = SimpleNamespace(last_seen_at=synced)                      # seen this sync
    removed = SimpleNamespace(last_seen_at=synced - dt.timedelta(days=30))  # gone since
    unknown = SimpleNamespace(last_seen_at=None)                        # never synced → keep
    assert invoicing._reseller_present(present, panel) is True
    assert invoicing._reseller_present(removed, panel) is False
    assert invoicing._reseller_present(unknown, panel) is True


# ------------------------------------------------ backup must have both collections
def test_backup_fetch_rejects_partial(monkeypatch):
    import asyncio

    from app.services.panel_client.backup_json import BackupJsonClient

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class _Client:
        def __init__(self, payload):
            self._payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, auth=None):
            return _Resp(self._payload)

    panel = SimpleNamespace(
        owner_uuid="u", base_secret_url="https://h/p", backup_url="https://h/p/admin/backup",
        key="p",
    )

    async def run(payload):
        import app.services.panel_client.backup_json as mod
        monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kw: _Client(payload))
        return await BackupJsonClient().fetch_backup(panel)

    # users-only (admins truncated away) → must raise, not accept a mass-admin-deletion.
    import pytest
    with pytest.raises(RuntimeError):
        asyncio.run(run({"users": [{"uuid": "x"}]}))
    # admins present but EMPTY list → also rejected.
    with pytest.raises(RuntimeError):
        asyncio.run(run({"admin_users": [], "users": []}))
    # both present (admins non-empty) → accepted.
    data = asyncio.run(run({"admin_users": [{"uuid": "a", "name": "Owner"}], "users": []}))
    assert len(data.admins) == 1 and data.users == []


# ------------------------------------------------ stale auto-rate fallback
def test_auto_rate_falls_back_when_stale():
    now = dt.datetime.now(dt.timezone.utc)
    fresh = now.isoformat()
    stale = (now - dt.timedelta(hours=72)).isoformat()
    assert rates._rate_is_fresh(fresh, 48) is True
    assert rates._rate_is_fresh(stale, 48) is False
    assert rates._rate_is_fresh(None, 48) is False
    assert rates._rate_is_fresh(stale, 0) is True  # 0 disables the check


def test_get_effective_rate_prefers_fresh_else_manual():
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.db import Base
    from app.services import settings_service

    async def run(auto_at_offset_h, expect):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            await settings_service.set_value(s, "rate_mode", "auto")
            await settings_service.set_value(s, "toman_per_usdt", 70000)      # manual fallback
            await settings_service.set_value(s, "toman_per_usdt_auto", 95000)  # live
            await settings_service.set_value(s, "rate_max_age_hours", 48)
            stamp = (dt.datetime.now(dt.timezone.utc)
                     - dt.timedelta(hours=auto_at_offset_h)).isoformat()
            await settings_service.set_value(s, "toman_per_usdt_auto_at", stamp)
            return await rates.get_effective_rate(s)
        await engine.dispose()

    assert asyncio.run(run(1, 95000)) == 95000     # fresh → live rate
    assert asyncio.run(run(72, 70000)) == 70000    # stale → manual fallback


# ------------------------------------------------ generate: skip + reconcile (integration)
def test_generate_skips_failed_panel_and_reconciles_zero_draft(tmp_path):
    import asyncio

    from sqlalchemy import select as _select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.db import Base
    from app.models import Invoice, Panel, Reseller
    from app.models.enums import InvoiceStatus
    from app.services.periods import month_period

    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'g.db'}")
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        now = dt.datetime.now(dt.timezone.utc)
        period = month_period(2026, 1)
        async with Session() as s:
            ok_panel = Panel(key="ok", host="h", proxy_path_enc="x", owner_uuid="o",
                             last_synced_at=now, status=PanelStatus.ok)
            bad_panel = Panel(key="bad", host="h", proxy_path_enc="x", owner_uuid="o",
                              last_synced_at=now, status=PanelStatus.error)
            s.add_all([ok_panel, bad_panel]); await s.flush()
            # A reseller on each panel; both present in the latest sync, neither has any users.
            r_ok = Reseller(panel_id=ok_panel.id, admin_uuid="a-ok", name="OK", last_seen_at=now)
            r_bad = Reseller(panel_id=bad_panel.id, admin_uuid="a-bad", name="BAD", last_seen_at=now)
            s.add_all([r_ok, r_bad]); await s.flush()
            # A leftover DRAFT for the OK reseller from a prior run (positive amount).
            stale = Invoice(reseller_id=r_ok.id, panel_id=ok_panel.id,
                            period_start=period.start, period_end=period.end,
                            period_label=period.label, usage_gb=5, amount_toman=5000,
                            status=InvoiceStatus.draft)
            # A DRAFT on the FAILED panel must be left untouched (we couldn't re-verify it).
            stale_bad = Invoice(reseller_id=r_bad.id, panel_id=bad_panel.id,
                                period_start=period.start, period_end=period.end,
                                period_label=period.label, usage_gb=5, amount_toman=5000,
                                status=InvoiceStatus.draft)
            s.add_all([stale, stale_bad]); await s.commit()

            summary = await invoicing.generate_invoices(s, period)
            assert any("bad" in p for p in summary.skipped_panels)
            assert summary.reconciled_zero >= 1
            # OK reseller's stale draft (now zero usage) is gone; the failed panel's draft stays.
            remaining = (await s.execute(_select(Invoice.id, Invoice.panel_id))).all()
            panel_ids = {pid for _id, pid in remaining}
            assert ok_panel.id not in panel_ids       # reconciled away
            assert bad_panel.id in panel_ids          # untouched (sync failed)
        await engine.dispose()

    asyncio.run(run())
