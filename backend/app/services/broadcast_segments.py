"""Invoice-scoped segmentation. All invoice conditions apply to the SAME invoice set.

Months are billing periods (Gregorian), never send dates or the month of a manual block.
No panel calls or writes are made by this module.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EnforcementAction, Invoice, Payment, PaymentSettlement, Reseller
from app.models.enums import (
    EnforcementActionStatus,
    EnforcementActionType,
    InvoiceStatus,
    PaymentStatus,
)
from app.schemas.broadcast import BroadcastFilters
from app.services import periods

OWED = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)


def debt_age(inv: Invoice) -> int | None:
    anchor = inv.deferred_until
    if anchor is None and inv.sent_at is not None:
        anchor = periods.to_local_date(inv.sent_at)
    return (periods.today() - anchor).days if anchor is not None else None


async def narrow(
    session: AsyncSession, roots: list[Reseller], audience: str, filters: BroadcastFilters,
) -> tuple[list[Reseller], dict[int, dict[str, Any]]]:
    roots = [r for r in roots if (not filters.panel_ids or r.panel_id in filters.panel_ids)
             and (filters.access_state is None or r.enforcement_state == filters.access_state)
             and r.id not in filters.excluded_reseller_ids]
    if filters.search.strip():
        needle = filters.search.strip().casefold()
        roots = [r for r in roots if needle in f"{r.name} {r.admin_uuid} {r.bot_chat_id or ''} {r.id}".casefold()]
    if not roots:
        return [], {}
    invoices = list((await session.execute(select(Invoice).where(
        Invoice.reseller_id.in_([r.id for r in roots]),
        Invoice.status.notin_((InvoiceStatus.draft, InvoiceStatus.canceled)),
    ))).scalars())
    scoped = [i for i in invoices
              if (filters.period_from is None or i.period_start.strftime("%Y-%m") >= filters.period_from)
              and (filters.period_to is None or i.period_start.strftime("%Y-%m") <= filters.period_to)]
    has_period = filters.period_from is not None or filters.period_to is not None
    ids = [i.id for i in scoped]
    # Pending payments can cover multiple invoices. Respect both the indexed bridge and
    # the legacy single-invoice pointer; an unallocated receipt cannot prove a month's match.
    pending = set((await session.execute(select(Payment.reseller_id).where(
        Payment.status == PaymentStatus.pending,
        *([or_(Payment.invoice_id.in_(ids), Payment.id.in_(select(PaymentSettlement.payment_id).where(
            PaymentSettlement.invoice_id.in_(ids))))] if has_period else []),
    ))).scalars())
    global_pending = pending
    if has_period and filters.exclude_pending:
        global_pending = set((await session.execute(select(Payment.reseller_id).where(
            Payment.status == PaymentStatus.pending))).scalars())
    # One person can own accounts on several panels. When excluding pending receipts,
    # exclude their linked chat across panels too, rather than reaching them via another account.
    pending_chats: set[int | None] = set()
    if filters.exclude_pending:
        pending_chats = set((await session.execute(select(Reseller.bot_chat_id).where(
            Reseller.id.in_(select(Payment.reseller_id).where(Payment.status == PaymentStatus.pending)),
            Reseller.bot_chat_id.is_not(None),
        ))).scalars())
    action_ids: set[int] = set()
    if has_period and audience in ("enforced", "frozen"):
        kind = (EnforcementActionType.disable_users if audience == "enforced"
                else EnforcementActionType.freeze)
        action_ids = set((await session.execute(select(EnforcementAction.reseller_id).where(
            EnforcementAction.invoice_id.in_(ids), EnforcementAction.action == kind,
            EnforcementAction.dry_run.is_(False),
            EnforcementAction.status.in_((EnforcementActionStatus.done, EnforcementActionStatus.partial)),
        ))).scalars())
    grouped: dict[int, list[Invoice]] = defaultdict(list)
    for inv in scoped:
        grouped[inv.reseller_id].append(inv)
    globally_owed = {i.reseller_id for i in invoices if i.status in OWED}
    selected = []
    metadata: dict[int, dict[str, Any]] = {}
    today = periods.today()
    for root in roots:
        rows = grouped[root.id]
        owed = [i for i in rows if i.status in OWED]
        qualifying = owed
        if audience == "overdue":
            qualifying = [i for i in owed if i.status in (InvoiceStatus.overdue, InvoiceStatus.enforced)
                          and (i.deferred_until is None or i.deferred_until <= today)]
        elif audience == "deferred":
            qualifying = [i for i in owed if i.deferred_until is not None and i.deferred_until > today]
        if audience in ("debtors", "overdue", "deferred") and not qualifying:
            continue
        if audience == "paid_up" and root.id in globally_owed:
            continue
        if audience == "pending_payment" and root.id not in pending:
            continue
        if has_period and (not rows or (audience in ("enforced", "frozen") and root.id not in action_ids)):
            continue
        if filters.exclude_pending and (root.id in global_pending or root.bot_chat_id in pending_chats):
            continue
        # Age constraints must hold for the very invoice that matched overdue/deferred,
        # not an unrelated older invoice on the same account. Unknown send dates fail closed.
        if filters.min_age_days is not None or filters.max_age_days is not None:
            qualifying = [i for i in qualifying if (age := debt_age(i)) is not None
                          and (filters.min_age_days is None or age >= filters.min_age_days)
                          and (filters.max_age_days is None or age <= filters.max_age_days)]
            if not qualifying:
                continue
        debt = float(sum(i.amount_toman for i in owed))
        if ((filters.min_debt is not None and (not owed or debt < filters.min_debt))
                or (filters.max_debt is not None and (not owed or debt > filters.max_debt))):
            continue
        ages = [a for i in owed if (a := debt_age(i)) is not None]
        metadata[root.id] = {
            "debt_toman": debt, "oldest_debt_days": max(ages) if ages else None,
            "periods": sorted({i.period_start.strftime("%Y-%m") for i in rows}),
            "access_state": root.enforcement_state.value,
        }
        selected.append(root)
    return selected, metadata
