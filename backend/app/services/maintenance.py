"""
Periodic retention sweep for the append-only LOG / audit tables.

Three tables grow without bound during normal operation and carry no operational
value once they age out:

  • ``sync_runs``           — one row per panel sync (every few hours, per panel).
  • ``delivery_log``        — one row per Telegram message attempt (invoice delivery,
                              reminders, warnings, payment acks, broadcasts).
  • ``enforcement_actions`` — one row per suspend/restore attempt, each carrying a
                              full user-enable + admin-limit JSON ``snapshot`` (these
                              are by far the largest rows in the database).

``prune_old_logs`` deletes rows older than ``log_retention_days`` (default 90) while
preserving anything still operationally needed:

  • ``delivery_log`` rows whose invoice is still OWED (sent/overdue/enforced) are kept
    regardless of age — the dunning job reads them to avoid re-sending a reminder it
    already sent, and to delete the prior message when an invoice is re-sent.
  • ``enforcement_actions`` still in flight (planned/running/partial) are kept
    regardless of age — they are live queue work, not history.

Billing and freshness never read these tables (they use ``Panel.last_synced_at``), so
pruning them can never affect an invoice. The financial ledger (``financial_records``,
``invoices``, ``payments``) is permanent and is NEVER touched here.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, cast

from sqlalchemy import CursorResult, delete, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    DeliveryLog,
    EndUserSnapshot,
    EnforcementAction,
    Invoice,
    Panel,
    SyncRun,
    UsageMeter,
)
from app.models.enums import EnforcementActionStatus, InvoiceStatus, PanelStatus
from app.services import settings_service
from app.services.periods import current_month, previous_month


def _months_ago_label(months: int) -> str:
    """The 'YYYY-MM' label `months` before the current month (lexicographically comparable to
    UsageMeter.period_label)."""
    start = current_month().start
    total = start.year * 12 + (start.month - 1) - months
    y, m = divmod(total, 12)
    return f"{y:04d}-{m + 1:02d}"

log = logging.getLogger("services.maintenance")

# Invoice statuses whose dunning cycle is still live — their delivery logs must survive.
_OWED = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)

# Enforcement-action states that are finished / audit-only and therefore safe to age out.
# Everything else (planned/running/partial) is active queue work and is always kept.
_TERMINAL_ACTIONS = (
    EnforcementActionStatus.done,
    EnforcementActionStatus.reverted,
    EnforcementActionStatus.dry_run,
    EnforcementActionStatus.failed,
)


async def prune_old_logs(
    session: AsyncSession, *, now: dt.datetime | None = None
) -> dict[str, int]:
    """Delete aged rows from the three log tables. Returns the per-table delete counts.

    Retention window is ``log_retention_days`` (a setting); ``<= 0`` disables pruning.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    # NOTE: a missing/invalid value falls back to 90, but an explicit 0 must stay 0 (it
    # disables pruning) — so do NOT use `value or 90`, which would turn 0 into 90.
    raw = await settings_service.get(session, "log_retention_days", 90)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = 90
    if days <= 0:
        return {"sync_runs": 0, "delivery_log": 0, "enforcement_actions": 0, "retention_days": 0}
    cutoff = now - dt.timedelta(days=days)

    async def _delete(stmt: Any) -> int:
        result = await session.execute(stmt.execution_options(synchronize_session=False))
        return cast("CursorResult[Any]", result).rowcount or 0

    # sync_runs — pure audit. Billing/freshness reads Panel.last_synced_at, not this table,
    # and the Panels UI only shows the latest handful, so any aged row is safe to drop.
    sync_runs = await _delete(delete(SyncRun).where(SyncRun.started_at < cutoff))

    # delivery_log — drop aged rows EXCEPT those tied to an invoice that is still owed
    # (kept for dunning's reminder de-dup + resend cleanup). Rows with no invoice
    # (broadcasts, generic notices) and rows of paid/canceled/draft/deleted invoices age out.
    owed_invoice_ids = select(Invoice.id).where(Invoice.status.in_(_OWED))
    delivery_log = await _delete(
        delete(DeliveryLog).where(
            DeliveryLog.created_at < cutoff,
            or_(
                DeliveryLog.invoice_id.is_(None),
                DeliveryLog.invoice_id.notin_(owed_invoice_ids),
            ),
        )
    )

    # enforcement_actions — drop aged terminal/audit rows (incl. dry-run rows that accumulate
    # when enforcement is in its default dry-run mode). Active queue work is preserved.
    enforcement_actions = await _delete(
        delete(EnforcementAction).where(
            EnforcementAction.created_at < cutoff,
            EnforcementAction.status.in_(_TERMINAL_ACTIONS),
        )
    )
    await session.commit()

    counts = {
        "sync_runs": sync_runs,
        "delivery_log": delivery_log,
        "enforcement_actions": enforcement_actions,
        "retention_days": days,
    }
    if counts["sync_runs"] or counts["delivery_log"] or counts["enforcement_actions"]:
        log.info("Log retention sweep (>%dd): %s", days, counts)
    return counts


async def prune_stale_snapshots(session: AsyncSession) -> dict[str, int]:
    """Delete end-user snapshots of users REMOVED from Hiddify whose creation month is older than
    the previous billing month, plus any now-orphaned usage_meters.

    A removed user (its `last_synced_at` is older than the panel's latest sync) is kept ON PURPOSE
    while its creation month can still be invoiced — the invoice engine bills mid-month deletions
    from this lingering snapshot. Once its `start_date` is older than the previous month, no current
    or next invoice can reference it, so it's pure bloat and safe to drop. The current + previous
    month are always preserved. `usage_meters` has no FK, so meters left behind by a deleted
    snapshot (or a deleted panel) are swept too. Billing/financial tables are never touched."""
    async def _delete(stmt: Any) -> int:
        result = await session.execute(stmt.execution_options(synchronize_session=False))
        return cast("CursorResult[Any]", result).rowcount or 0

    keep_from = previous_month().start  # first day of the previous month (a date)
    stale_old = (
        select(EndUserSnapshot.id)
        .join(Panel, Panel.id == EndUserSnapshot.panel_id)
        .where(
            Panel.status == PanelStatus.ok,
            Panel.last_synced_at.is_not(None),
            EndUserSnapshot.last_synced_at.is_not(None),
            EndUserSnapshot.last_synced_at < Panel.last_synced_at,  # not seen in the latest sync
            or_(
                EndUserSnapshot.start_date.is_(None),               # never billable (no create date)
                EndUserSnapshot.start_date < keep_from,             # older than the previous month
            ),
        )
    )
    snapshots = await _delete(delete(EndUserSnapshot).where(EndUserSnapshot.id.in_(stale_old)))

    # usage_meters has NO foreign key → meters orphaned by the deletion above (or by a deleted
    # panel/user) are never cleaned automatically. Drop any meter with no matching snapshot.
    orphan_meter = exists().where(
        EndUserSnapshot.panel_id == UsageMeter.panel_id,
        EndUserSnapshot.user_uuid == UsageMeter.user_uuid,
    )
    meters = await _delete(delete(UsageMeter).where(~orphan_meter))

    # usage_meters keeps one row per active user per month forever, so old periods accumulate even
    # for present users. Those periods are already locked into invoices + the financial ledger, so
    # drop meters older than `meter_retention_months` (keep the current + recent months). 0 disables.
    try:
        keep_months = int(await settings_service.get(session, "meter_retention_months", 6))
    except (TypeError, ValueError):
        keep_months = 6
    old_meters = 0
    if keep_months > 0:
        cutoff_label = _months_ago_label(keep_months)
        old_meters = await _delete(
            delete(UsageMeter).where(UsageMeter.period_label < cutoff_label)
        )
    await session.commit()

    counts = {"stale_snapshots": snapshots, "orphan_meters": meters, "old_meters": old_meters}
    if snapshots or meters or old_meters:
        log.info("Stale-snapshot retention sweep (keep >= %s): %s", keep_from, counts)
    return counts
