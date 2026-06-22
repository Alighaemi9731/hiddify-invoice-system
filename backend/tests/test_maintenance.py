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


def test_prune_stale_snapshots(tmp_path):
    """Removed-from-Hiddify users older than the previous billing month are dropped; present users
    and recent (current/previous-month) removed users are kept; orphaned usage_meters are swept."""
    import datetime as dt2

    from app.models import EndUserSnapshot, UsageMeter
    from app.models import Panel as P
    from app.models.enums import PanelStatus
    from app.services.periods import current_month, previous_month

    now = dt2.datetime.now()  # naive → SQLite round-trips naive, comparisons stay consistent
    stale_seen = now - dt2.timedelta(days=1)        # older than the panel's latest sync → removed
    keep_from = previous_month().start
    old_date = keep_from - dt2.timedelta(days=40)   # well before the previous month

    def snap(uid, *, seen, start):
        return EndUserSnapshot(panel_id=1, user_uuid=uid, added_by_uuid="a",
                               usage_limit_gb=10, start_date=start, last_synced_at=seen,
                               enable=True, is_active=True, name=uid)

    async def body(s):
        s.add(P(id=1, key="p1", host="p1", proxy_path_enc="x", owner_uuid="o",
                status=PanelStatus.ok, last_synced_at=now))
        s.add_all([
            snap("present", seen=now, start=old_date),               # not stale → kept
            snap("stale_recent", seen=stale_seen, start=current_month().start),  # kept (recent)
            snap("stale_prev", seen=stale_seen, start=keep_from),    # kept (previous month edge)
            snap("stale_old", seen=stale_seen, start=old_date),      # DELETED
            snap("stale_nullstart", seen=stale_seen, start=None),    # DELETED (never billable)
        ])
        s.add_all([
            UsageMeter(panel_id=1, user_uuid="present", period_label="2026-06"),     # kept
            UsageMeter(panel_id=1, user_uuid="stale_old", period_label="2026-06"),   # orphan → gone
            UsageMeter(panel_id=1, user_uuid="ghost", period_label="2026-06"),       # no snapshot → gone
        ])
        await s.commit()

        counts = await maintenance.prune_stale_snapshots(s)
        assert counts == {"stale_snapshots": 2, "orphan_meters": 2, "old_meters": 0}

        remaining = {u for (u,) in await s.execute(select(EndUserSnapshot.user_uuid))}
        assert remaining == {"present", "stale_recent", "stale_prev"}
        meters = (await s.execute(select(func.count(UsageMeter.id)))).scalar_one()
        assert meters == 1
    _run(body, tmp_path, "stale.db")
