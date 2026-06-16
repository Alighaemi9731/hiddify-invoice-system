"""
Targeted broadcast to bot-registered resellers.

The audience always starts from ONE base set — the top-level resellers shown in the
«نمایندگان» main list that are NOT exempt from billing and are present on an active panel
(`reseller_stats.load_billable_roots`). Every filter only narrows that set down; it never
sends to sub-resellers, billing-exempt resellers, or removed admins.

Nothing here is written to the database — the per-recipient report is returned to the
caller and shown live, so broadcasting never bloats the DB.
"""
from __future__ import annotations

import logging
from typing import TypedDict

from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.telegram import build_bot
from app.models import EndUserSnapshot, Invoice, Panel, Reseller
from app.models.enums import InvoiceStatus
from app.services import reseller_stats

log = logging.getLogger("broadcast")

_OWED = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)

# Audience filters (each applied ON TOP of the base set). `panel_id` is an independent,
# combinable restriction (one panel, or all panels when None).
AUDIENCES = ("all", "debtors", "zero_sale", "few_active", "invoice_below")


class Recipient(TypedDict):
    reseller_id: int
    name: str
    panel: str
    chat_id: int | None
    status: str  # pending | sent | blocked | failed | unregistered


class BroadcastResult(TypedDict):
    audience: str
    panel_id: int | None
    threshold: float | None
    matched: int        # resellers matching the filter (reachable + unregistered)
    total: int          # reachable recipients (registered in the bot)
    unregistered: int   # matched the filter but never started the bot → cannot be reached
    sent: int
    blocked: int
    failed: int
    recipients: list[Recipient]  # reachable recipients with their final status
    skipped: list[Recipient]     # matched but unregistered (chat_id is None)


async def _active_counts(session: AsyncSession) -> dict[tuple[int, str], int]:
    """ACTIVE end-users each admin created, keyed by (panel_id, admin_uuid) — the same metric the
    Resellers tab's capacity column shows (this admin's own users, enabled AND is_active)."""
    rows = await session.execute(
        select(EndUserSnapshot.panel_id, EndUserSnapshot.added_by_uuid, func.count())
        .where(
            EndUserSnapshot.added_by_uuid.is_not(None),
            EndUserSnapshot.enable.is_(True),
            EndUserSnapshot.is_active.is_(True),
        )
        .group_by(EndUserSnapshot.panel_id, EndUserSnapshot.added_by_uuid)
    )
    return {(pid, uuid): int(n) for pid, uuid, n in rows.all()}


async def _bundle_amounts(
    session: AsyncSession, panel_id: int | None
) -> dict[int, tuple[float, float]]:
    """Current-month (so far) {root_id: (billable_gb, amount_toman)} for every billable bundle —
    the same numbers the invoice would carry. Used by the zero-sale and invoice-below filters."""
    from app.services import invoicing
    from app.services.periods import current_month

    out: dict[int, tuple[float, float]] = {}
    for _panel, b in await invoicing.preview_bundles(session, current_month(), panel_id=panel_id):
        base = round(b.total_gb * b.price_per_gb)
        amount = float(b.min_sale_toman) if (0 < base < (b.min_sale_toman or 0)) else float(base)
        out[b.root.id] = (b.total_gb, amount)
    return out


async def _matching_roots(
    session: AsyncSession, audience: str, panel_id: int | None, threshold: float | None
) -> list[Reseller]:
    """The base set narrowed by the chosen filter (and the optional single-panel restriction)."""
    roots = await reseller_stats.load_billable_roots(session, panel_id)
    if audience == "debtors":
        owed = set((await session.execute(
            select(Invoice.reseller_id).where(Invoice.status.in_(_OWED)).distinct()
        )).scalars().all())
        return [r for r in roots if r.id in owed]
    if audience == "few_active":
        limit = int(threshold or 0)
        active = await _active_counts(session)
        return [r for r in roots if active.get((r.panel_id, r.admin_uuid), 0) < limit]
    if audience in ("zero_sale", "invoice_below"):
        amounts = await _bundle_amounts(session, panel_id)
        if audience == "zero_sale":
            return [r for r in roots if amounts.get(r.id, (0.0, 0.0))[0] <= 0]
        below = float(threshold or 0)
        return [r for r in roots if amounts.get(r.id, (0.0, 0.0))[1] < below]
    return roots  # "all"


async def resolve_recipients(
    session: AsyncSession, audience: str, panel_id: int | None, threshold: float | None
) -> tuple[list[Recipient], list[Recipient]]:
    """(reachable, unregistered): the matched roots split by whether they can be messaged.
    Reachable recipients are de-duplicated by chat_id (one person on two panels → one message)."""
    roots = await _matching_roots(session, audience, panel_id, threshold)
    panel_keys = {p.id: p.key for p in (await session.execute(select(Panel))).scalars().all()}
    reachable: dict[int, Recipient] = {}
    unregistered: list[Recipient] = []
    for r in roots:
        rec = Recipient(
            reseller_id=r.id, name=r.name or "—",
            panel=panel_keys.get(r.panel_id, "?"), chat_id=r.bot_chat_id, status="pending",
        )
        if r.bot_chat_id:
            reachable.setdefault(r.bot_chat_id, rec)
        else:
            rec["status"] = "unregistered"
            unregistered.append(rec)
    return list(reachable.values()), unregistered


def _empty(audience: str, panel_id: int | None, threshold: float | None) -> BroadcastResult:
    return BroadcastResult(
        audience=audience, panel_id=panel_id, threshold=threshold,
        matched=0, total=0, unregistered=0, sent=0, blocked=0, failed=0,
        recipients=[], skipped=[],
    )


async def preview(
    session: AsyncSession, *, audience: str = "all",
    panel_id: int | None = None, threshold: float | None = None,
) -> BroadcastResult:
    """Resolve the recipient list WITHOUT sending, so the owner can verify the filter first."""
    reachable, unregistered = await resolve_recipients(session, audience, panel_id, threshold)
    res = _empty(audience, panel_id, threshold)
    res["recipients"] = reachable
    res["skipped"] = unregistered
    res["total"] = len(reachable)
    res["unregistered"] = len(unregistered)
    res["matched"] = len(reachable) + len(unregistered)
    return res


async def broadcast(
    session: AsyncSession, text: str, *, audience: str = "all",
    panel_id: int | None = None, threshold: float | None = None,
) -> BroadcastResult:
    """Send `text` to the resolved recipients and return a full per-recipient report."""
    reachable, unregistered = await resolve_recipients(session, audience, panel_id, threshold)
    res = _empty(audience, panel_id, threshold)
    res["skipped"] = unregistered
    res["unregistered"] = len(unregistered)
    res["total"] = len(reachable)
    res["matched"] = len(reachable) + len(unregistered)
    if not text.strip() or not reachable:
        res["recipients"] = reachable
        return res

    bot = await build_bot(session)
    if bot is None:
        for rec in reachable:
            rec["status"] = "failed"
        res["failed"] = len(reachable)
        res["recipients"] = reachable
        return res
    try:
        for rec in reachable:
            cid = rec["chat_id"]
            if cid is None:  # reachable always has a chat_id; this just satisfies the type checker
                continue
            try:
                await bot.send_message(cid, text)
                rec["status"] = "sent"
                res["sent"] += 1
            except TelegramForbiddenError:
                rec["status"] = "blocked"
                res["blocked"] += 1
            except Exception:  # noqa: BLE001
                rec["status"] = "failed"
                res["failed"] += 1
    finally:
        await bot.session.close()
    res["recipients"] = reachable
    log.info("Broadcast %s panel=%s: sent=%s blocked=%s failed=%s unreg=%s",
             audience, panel_id, res["sent"], res["blocked"], res["failed"], res["unregistered"])
    return res
