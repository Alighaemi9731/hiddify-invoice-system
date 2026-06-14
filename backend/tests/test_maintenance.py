"""Log-retention sweep (app.services.maintenance.prune_old_logs).

Verifies the three append-only log tables are pruned by age while operationally-live
rows are preserved:
  • sync_runs: aged rows go, recent stay.
  • delivery_log: aged rows of settled/orphan invoices go; an OWED invoice's reminder
    rows stay regardless of age (dunning de-dup); recent rows stay.
  • enforcement_actions: aged terminal rows (done/reverted/dry_run/failed) go; aged
    in-flight rows (planned/running/partial) stay; recent rows stay.
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/maint.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import (  # noqa: E402
    DeliveryLog,
    EnforcementAction,
    Invoice,
    Panel,
    Reseller,
    SyncRun,
)
from app.models.enums import (  # noqa: E402
    DeliveryKind,
    DeliveryStatus,
    EnforcementActionStatus,
    EnforcementActionType,
    InvoiceStatus,
    SyncStatus,
)
from app.services import maintenance  # noqa: E402


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


def _invoice(reseller_id, *, label, status):
    y, m = (int(x) for x in label.split("-"))
    start = dt.date(y, m, 1)
    end = dt.date(y + (m // 12), (m % 12) + 1, 1) - dt.timedelta(days=1)
    return Invoice(
        reseller_id=reseller_id, panel_id=1, period_start=start, period_end=end,
        period_label=label, status=status, sent_at=dt.datetime.now(dt.timezone.utc),
    )


async def _count(s, model) -> int:
    return (await s.execute(select(func.count()).select_from(model))).scalar_one()


def test_prune_keeps_recent_and_live_drops_aged(tmp_path):
    async def body(s):
        now = dt.datetime.now(dt.timezone.utc)
        old = now - dt.timedelta(days=120)   # past the 90-day default window
        recent = now - dt.timedelta(days=10)  # inside the window

        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R")
        s.add(r)
        await s.flush()

        owed = _invoice(r.id, label="2026-01", status=InvoiceStatus.sent)     # live
        paid = _invoice(r.id, label="2026-02", status=InvoiceStatus.paid)     # settled
        s.add_all([owed, paid])
        await s.flush()

        # --- sync_runs: 1 aged (prune), 1 recent (keep) ---
        s.add(SyncRun(panel_id=1, status=SyncStatus.success, started_at=old))
        s.add(SyncRun(panel_id=1, status=SyncStatus.success, started_at=recent))

        # --- delivery_log ---
        # aged reminder for an OWED invoice -> KEPT (dunning de-dup needs it)
        s.add(DeliveryLog(reseller_id=r.id, invoice_id=owed.id, kind=DeliveryKind.reminder1,
                          status=DeliveryStatus.sent, created_at=old))
        # aged reminder for a PAID invoice -> pruned
        s.add(DeliveryLog(reseller_id=r.id, invoice_id=paid.id, kind=DeliveryKind.reminder1,
                          status=DeliveryStatus.sent, created_at=old))
        # aged broadcast (no invoice) -> pruned
        s.add(DeliveryLog(reseller_id=r.id, invoice_id=None, kind=DeliveryKind.generic,
                          status=DeliveryStatus.sent, created_at=old))
        # recent invoice delivery -> kept
        s.add(DeliveryLog(reseller_id=r.id, invoice_id=paid.id, kind=DeliveryKind.invoice,
                          status=DeliveryStatus.sent, created_at=recent))

        # --- enforcement_actions ---
        # aged terminal rows -> pruned
        s.add(EnforcementAction(reseller_id=r.id, action=EnforcementActionType.disable_users,
                                status=EnforcementActionStatus.done, created_at=old))
        s.add(EnforcementAction(reseller_id=r.id, action=EnforcementActionType.disable_users,
                                status=EnforcementActionStatus.dry_run, created_at=old))
        s.add(EnforcementAction(reseller_id=r.id, action=EnforcementActionType.restore,
                                status=EnforcementActionStatus.reverted, created_at=old))
        # aged but still in-flight -> KEPT (live queue work)
        s.add(EnforcementAction(reseller_id=r.id, action=EnforcementActionType.disable_users,
                                status=EnforcementActionStatus.planned, created_at=old))
        # recent terminal -> kept
        s.add(EnforcementAction(reseller_id=r.id, action=EnforcementActionType.disable_users,
                                status=EnforcementActionStatus.done, created_at=recent))
        await s.commit()

        counts = await maintenance.prune_old_logs(s, now=now)

        assert counts["sync_runs"] == 1
        assert counts["delivery_log"] == 2          # paid-reminder + broadcast
        assert counts["enforcement_actions"] == 3   # done + dry_run + reverted (aged)
        assert counts["retention_days"] == 90

        assert await _count(s, SyncRun) == 1
        assert await _count(s, DeliveryLog) == 2     # owed-reminder + recent invoice
        assert await _count(s, EnforcementAction) == 2  # planned (aged) + recent done

        # the surviving delivery rows are exactly the owed-invoice reminder and the recent one
        kinds = set((await s.execute(select(DeliveryLog.kind))).scalars().all())
        assert kinds == {DeliveryKind.reminder1, DeliveryKind.invoice}

    _run(body, tmp_path, "maint.db")


def test_retention_zero_disables_pruning(tmp_path):
    async def body(s):
        from app.services import settings_service
        await settings_service.set_value(s, "log_retention_days", 0)
        s.add(SyncRun(panel_id=1, status=SyncStatus.success,
                      started_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=9999)))
        await s.commit()

        counts = await maintenance.prune_old_logs(s)
        assert counts["retention_days"] == 0
        assert await _count(s, SyncRun) == 1  # nothing pruned

    _run(body, tmp_path, "maint_off.db")
