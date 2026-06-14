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

from sqlalchemy import CursorResult, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DeliveryLog, EnforcementAction, Invoice, SyncRun
from app.models.enums import EnforcementActionStatus, InvoiceStatus
from app.services import settings_service

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
