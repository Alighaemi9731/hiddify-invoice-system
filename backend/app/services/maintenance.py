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
import os
from typing import Any, cast

from sqlalchemy import CursorResult, and_, delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BotUser,
    DeliveryLog,
    EndUserSnapshot,
    EnforcementAction,
    Invoice,
    Panel,
    Payment,
    PortalLoginNonce,
    Reseller,
    StorefrontCustomer,
    StorefrontOperation,
    StorefrontOrder,
    StorefrontWalletTxn,
    SyncRun,
    UsageMeter,
    UsageMeterEvent,
)
from app.models.enums import (
    EnforcementActionStatus,
    EnforcementActionType,
    EnforcementState,
    InvoiceStatus,
    PanelStatus,
    PaymentStatus,
)
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
    raw = await settings_service.get(session, "log_retention_days", 60)
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
    # EXCEPTION: the newest live disable/freeze action of any reseller who is STILL
    # enforced/frozen is the restore SOURCE — queue_restore reads its snapshot to know which
    # users/limits to bring back. Pruning it would make restore permanently impossible (a
    # freeze is open-ended by design, and a >90-day suspension is exactly the reseller who
    # eventually pays). Mirrors queue_restore's source selection (latest live disable/freeze
    # in a restorable status; max(id) ≡ latest since ids are monotonic with created_at).
    protected_restore_sources = (
        select(func.max(EnforcementAction.id))
        .join(Reseller, Reseller.id == EnforcementAction.reseller_id)
        .where(
            Reseller.enforcement_state != EnforcementState.active,
            EnforcementAction.action.in_(
                [EnforcementActionType.disable_users, EnforcementActionType.freeze]
            ),
            EnforcementAction.dry_run.is_(False),
            EnforcementAction.status.in_(
                [
                    EnforcementActionStatus.planned,
                    EnforcementActionStatus.partial,
                    EnforcementActionStatus.done,
                    EnforcementActionStatus.failed,
                ]
            ),
        )
        .group_by(EnforcementAction.reseller_id)
    )
    enforcement_actions = await _delete(
        delete(EnforcementAction).where(
            EnforcementAction.created_at < cutoff,
            EnforcementAction.status.in_(_TERMINAL_ACTIONS),
            EnforcementAction.id.notin_(protected_restore_sources),
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
    # The per-event log follows its meter's lifecycle exactly: an event whose aggregate
    # meter is gone must never linger (a stale event could otherwise poison the renewal
    # enumeration of a rebuilt meter in the same month).
    orphan_event = exists().where(
        UsageMeter.panel_id == UsageMeterEvent.panel_id,
        UsageMeter.user_uuid == UsageMeterEvent.user_uuid,
        UsageMeter.period_label == UsageMeterEvent.period_label,
    )
    await _delete(delete(UsageMeterEvent).where(~orphan_event))

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
        await _delete(
            delete(UsageMeterEvent).where(UsageMeterEvent.period_label < cutoff_label)
        )
    await session.commit()

    counts = {"stale_snapshots": snapshots, "orphan_meters": meters, "old_meters": old_meters}
    if snapshots or meters or old_meters:
        log.info("Stale-snapshot retention sweep (keep >= %s): %s", keep_from, counts)
    return counts


async def prune_stale_storefront(
    session: AsyncSession, *, now: dt.datetime | None = None
) -> dict[str, int]:
    """Purge storefront bloat so the DB doesn't grow from tire-kickers.

    Deletes:
      • Customers with **no financial footprint** — zero wallet balance, no provisioned/disabled order,
        AND no confirmed top-up — inactive for ``storefront_stale_customer_days`` (default 90; 0 = off).
        Such a customer never successfully paid (net balance 0), so the WHOLE row goes — their orders,
        wallet txns and proof files included. (These are reseller↔customer records; the owner↔reseller
        billing ledger — financial_records/invoices/payments — is a different subsystem and is untouched.)
      • Orphan junk for KEPT customers: failed/deleted orders and rejected top-ups older than the window.

    For a KEPT customer (positive balance, a live service, or a confirmed top-up) nothing financial is
    pruned: confirmed top-ups, purchase/refund/manual txns, and provisioned/disabled orders all survive.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    raw = await settings_service.get(session, "storefront_stale_customer_days", 90)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = 90
    if days <= 0:
        return {
            "customers": 0, "junk_orders": 0, "junk_topups": 0,
            "pending_operations": 0, "retention_days": 0,
        }
    cutoff = now - dt.timedelta(days=days)

    async def _delete(stmt: Any) -> int:
        result = await session.execute(stmt.execution_options(synchronize_session=False))
        return cast("CursorResult[Any]", result).rowcount or 0

    has_live_order = exists().where(
        StorefrontOrder.customer_id == StorefrontCustomer.id,
        StorefrontOrder.status.in_(("provisioned", "disabled")),
    )
    has_confirmed_topup = exists().where(
        StorefrontWalletTxn.customer_id == StorefrontCustomer.id,
        StorefrontWalletTxn.kind == "topup",
        StorefrontWalletTxn.status == "confirmed",
    )
    activity = func.coalesce(StorefrontCustomer.last_seen_at, StorefrontCustomer.created_at)
    stale_ids = list((await session.execute(
        select(StorefrontCustomer.id).where(
            StorefrontCustomer.wallet_balance_toman <= 0,
            ~has_live_order,
            ~has_confirmed_topup,
            activity < cutoff,
        ).limit(5000)
    )).scalars().all())

    # Remove proof files for everything we're about to delete (stale customers' txns + aged rejected
    # top-ups), then the rows. Files are the real disk bloat.
    proof_filter = and_(StorefrontWalletTxn.status == "rejected", StorefrontWalletTxn.created_at < cutoff)
    if stale_ids:
        proof_filter = or_(proof_filter, StorefrontWalletTxn.customer_id.in_(stale_ids))
    for path in (await session.execute(
        select(StorefrontWalletTxn.proof_path).where(
            StorefrontWalletTxn.proof_path.is_not(None), proof_filter)
    )).scalars().all():
        if not path:
            continue
        try:
            os.remove(path)
        except OSError:
            pass

    customers = 0
    if stale_ids:
        # Explicit child-first deletes (don't depend on DB cascade being enabled, e.g. SQLite PRAGMA).
        await _delete(delete(StorefrontOperation).where(
            StorefrontOperation.customer_id.in_(stale_ids)))
        await _delete(delete(StorefrontWalletTxn).where(StorefrontWalletTxn.customer_id.in_(stale_ids)))
        await _delete(delete(StorefrontOrder).where(StorefrontOrder.customer_id.in_(stale_ids)))
        customers = await _delete(delete(StorefrontCustomer).where(StorefrontCustomer.id.in_(stale_ids)))

    junk_orders = await _delete(delete(StorefrontOrder).where(
        StorefrontOrder.status.in_(("failed", "deleted")), StorefrontOrder.created_at < cutoff))
    junk_topups = await _delete(delete(StorefrontWalletTxn).where(
        StorefrontWalletTxn.kind == "topup", StorefrontWalletTxn.status == "rejected",
        StorefrontWalletTxn.created_at < cutoff))
    # Confirmation cards reserve their operation token before delivery. Cards that were never clicked
    # remain `pending`; age only those abandoned reservations out. Terminal/in-progress money actions
    # are retained so idempotent replay and recovery remain durable.
    pending_operations = await _delete(delete(StorefrontOperation).where(
        StorefrontOperation.status == "pending", StorefrontOperation.created_at < cutoff))
    await session.commit()

    counts = {
        "customers": customers, "junk_orders": junk_orders, "junk_topups": junk_topups,
        "pending_operations": pending_operations,
    }
    if any(counts.values()):
        log.info("Storefront retention sweep (>%dd inactive): %s", days, counts)
    return counts


def _sweep_old_files(root: str, cutoff_ts: float) -> int:
    """Delete files under `root` whose mtime is older than `cutoff_ts`, then any dirs left empty.
    Best-effort (a locked/racing file is skipped). Returns the count of files removed."""
    removed = 0
    if not os.path.isdir(root):
        return 0
    for dirpath, _dirs, files in os.walk(root, topdown=False):
        for name in files:
            path = os.path.join(dirpath, name)
            try:
                if os.path.getmtime(path) < cutoff_ts:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
        try:
            if dirpath != root and not os.listdir(dirpath):
                os.rmdir(dirpath)
        except OSError:
            pass
    return removed


async def prune_owner_data(
    session: AsyncSession, *, now: dt.datetime | None = None
) -> dict[str, int]:
    """Owner-side disk + PII hygiene (the ledger and pending work are never touched):

      • Expired ``portal_login_nonce`` rows (always — they're dead once past ``expires_at``).
      • Payment PROOF FILES of terminal (confirmed/rejected) payments older than the window are
        deleted and their ``proof_path`` cleared — the DB row (the money fact) is KEPT; only the
        screenshot file, needed just for the one-time manual review, is removed.
      • Invoice PDF files on disk older than the window — they're regenerated on demand from the
        persisted invoice lines, so they're pure cache.
      • ``bot_users`` tire-kickers (not linked to a registered reseller) inactive past the window.

    Gated by ``owner_data_retention_days`` (default 180; 0 = off for the file/bot-user parts).
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    counts = {"nonces": 0, "proof_files": 0, "invoice_pdfs": 0, "bot_users": 0}

    async def _delete(stmt: Any) -> int:
        result = await session.execute(stmt.execution_options(synchronize_session=False))
        return cast("CursorResult[Any]", result).rowcount or 0

    # Expired one-time portal login nonces — dead rows, cleaned regardless of the retention window.
    counts["nonces"] = await _delete(
        delete(PortalLoginNonce).where(PortalLoginNonce.expires_at < now))

    raw = await settings_service.get(session, "owner_data_retention_days", 180)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = 180
    if days > 0:
        cutoff = now - dt.timedelta(days=days)
        # Payment proof files of terminal payments: delete the file, clear the path, keep the row.
        terminal = (PaymentStatus.confirmed, PaymentStatus.rejected)
        stale_proofs = (await session.execute(
            select(Payment).where(
                Payment.proof_path.is_not(None),
                Payment.status.in_(terminal),
                func.coalesce(Payment.verified_at, Payment.created_at) < cutoff,
            )
        )).scalars().all()
        for pay in stale_proofs:
            path = pay.proof_path
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
            pay.proof_path = None
            counts["proof_files"] += 1

        # Invoice PDFs are regenerated on demand → pure cache; drop old ones.
        counts["invoice_pdfs"] = _sweep_old_files("data/invoices", cutoff.timestamp())

        # bot_users tire-kickers: neither seen nor kicked recently, and NOT a registered reseller.
        registered = select(Reseller.bot_chat_id).where(Reseller.bot_chat_id.is_not(None))
        counts["bot_users"] = await _delete(
            delete(BotUser).where(
                func.coalesce(BotUser.last_seen_at, BotUser.created_at) < cutoff,
                func.coalesce(BotUser.last_kicked_at, BotUser.created_at) < cutoff,
                BotUser.telegram_id.notin_(registered),
            )
        )
    await session.commit()

    if any(counts.values()):
        log.info("Owner-data hygiene sweep: %s", counts)
    return counts
