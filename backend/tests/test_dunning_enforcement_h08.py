"""H08 — enforcement & dunning correctness set.

- A hard-failed live suspend is reset to planned when dunning re-requests it (mirrors the
  restore reset), so a stuck suspension isn't blocked forever.
- sync does not overwrite panel_max_* while a reseller is suspended/frozen (would feed the
  restore-from-DB fallback the zeroed limits).
- revert_to_draft clears the reminder DeliveryLog rows so a re-sent invoice gets a fresh
  dunning cycle.
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/h08.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import (  # noqa: E402
    DeliveryLog,
    EnforcementAction,
    Invoice,
    Panel,
    Reseller,
)
from app.models.enums import (  # noqa: E402
    DeliveryKind,
    DeliveryStatus,
    EnforcementActionStatus,
    EnforcementActionType,
    EnforcementState,
    InvoiceStatus,
)


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


def test_failed_suspend_requeued_on_next_dunning_tick(tmp_path):
    """queue_enforcement resets a hard-failed suspend for the same invoice back to planned
    (cleared attempts), so the worker picks it up again instead of it blocking forever."""
    from app.services import enforcement, settings_service

    async def body(s):
        await settings_service.set_value(s, "enforcement_enabled", True)
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R",
                     enforcement_state=EnforcementState.active)
        s.add(r)
        await s.flush()
        inv = Invoice(reseller_id=r.id, panel_id=1, period_start=dt.date(2026, 1, 1),
                      period_end=dt.date(2026, 1, 28), period_label="2026-01",
                      usage_gb=10, amount_toman=1000, amount_usdt=1,
                      status=InvoiceStatus.enforced,
                      sent_at=dt.datetime.now(dt.timezone.utc))
        s.add(inv)
        await s.flush()
        failed = EnforcementAction(
            reseller_id=r.id, invoice_id=inv.id,
            action=EnforcementActionType.disable_users, dry_run=False,
            status=EnforcementActionStatus.failed, error="panel unreachable",
            snapshot={"users": {"u0": "A"}, "admins": ["A"],
                      "progress": {"user_attempts": {"u0": 5}, "users_failed": {"u0": "x"},
                                   "admin_attempts": {}, "admins_failed": {}}},
        )
        s.add(failed)
        await s.commit()

        again = await enforcement.queue_enforcement(s, r, invoice_id=inv.id, dry_run=False)
        assert again.id == failed.id                       # same row reused
        assert again.status == EnforcementActionStatus.planned   # reset from failed
        prog = (again.snapshot or {}).get("progress") or {}
        assert prog.get("user_attempts") == {}             # attempts cleared

    _run(body, tmp_path, "f1.db")


def test_sync_preserves_panel_max_while_enforced(tmp_path):
    """A sync while the reseller is enforced must NOT record the zeroed on-panel limits."""
    from app.services import sync as sync_service
    from app.services.panel_client.base import PanelAdmin, PanelData

    async def body(s):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o"))
        r = Reseller(panel_id=1, admin_uuid="a", name="R",
                     panel_max_users=100, panel_max_active_users=100,
                     enforcement_state=EnforcementState.enforced)
        s.add(r)
        await s.commit()
        panel = await s.get(Panel, 1)
        # The panel now reports the ZEROED enforcement limits.
        data = PanelData(admins=[PanelAdmin(
            uuid="a", name="R", parent_admin_uuid=None, mode="agent", comment=None,
            telegram_id=None, max_users=0, max_active_users=0, can_add_admin=False)])
        await sync_service._upsert_resellers(s, panel, data, dt.datetime.now(dt.timezone.utc))
        await s.commit()
        await s.refresh(r)
        assert r.panel_max_users == 100                    # preserved, not overwritten with 0
        assert r.panel_max_active_users == 100

    _run(body, tmp_path, "f2.db")


def test_revert_to_draft_clears_reminder_logs(tmp_path):
    """revert_to_draft deletes the invoice's reminder DeliveryLog rows so a re-sent invoice
    gets a fresh dunning cycle (not straight to enforcement)."""
    from app.api import invoices as invoices_api

    async def body(s):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o"))
        r = Reseller(panel_id=1, admin_uuid="a", name="R")
        s.add(r)
        await s.flush()
        inv = Invoice(reseller_id=r.id, panel_id=1, period_start=dt.date(2026, 1, 1),
                      period_end=dt.date(2026, 1, 28), period_label="2026-01",
                      usage_gb=10, amount_toman=1000, amount_usdt=1,
                      status=InvoiceStatus.overdue,
                      sent_at=dt.datetime.now(dt.timezone.utc))
        s.add(inv)
        await s.flush()
        for kind in (DeliveryKind.reminder1, DeliveryKind.reminder2, DeliveryKind.warning):
            s.add(DeliveryLog(reseller_id=r.id, invoice_id=inv.id, kind=kind,
                              status=DeliveryStatus.sent))
        await s.commit()

        await invoices_api.revert_to_draft(inv.id, session=s)
        await s.refresh(inv)
        assert inv.status == InvoiceStatus.draft
        remaining = (
            await s.execute(select(DeliveryLog).where(DeliveryLog.invoice_id == inv.id))
        ).scalars().all()
        reminders = [d for d in remaining if d.kind in (
            DeliveryKind.reminder1, DeliveryKind.reminder2, DeliveryKind.warning)]
        assert reminders == []                             # reminder marks cleared

    _run(body, tmp_path, "f3.db")
