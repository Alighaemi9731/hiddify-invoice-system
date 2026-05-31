"""
Daily dunning: for each unpaid, delivered invoice, send reminders on a schedule and
finally enforce. Idempotent — each step is sent at most once per invoice (deduped via
the delivery log). Enforcement obeys the global dry-run switch.

Schedule (days after the invoice was sent; all editable in settings):
  reminder1_day → soft reminder
  reminder2_day → reminder
  warning_day   → hard warning (+ mark invoice overdue)
  enforcement_day → suspend the reseller (dry-run unless enforcement_enabled)
"""
from __future__ import annotations

import datetime as dt
import logging

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.telegram import build_bot
from app.models import DeliveryLog, EnforcementAction, Invoice, Payment, Reseller
from app.models.enums import (
    DeliveryKind,
    DeliveryStatus,
    EnforcementState,
    InvoiceStatus,
    PaymentStatus,
)
from app.services import enforcement, notifier, settings_service

log = logging.getLogger("dunning")

_ACTIVE = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)


async def _done_kinds(session: AsyncSession, invoice_id: int) -> set[str]:
    # Only count SUCCESSFULLY-sent deliveries as "done", so a reminder that failed
    # (transient Telegram error) or was unmatched (reseller hadn't registered yet) is
    # retried on the next run instead of being skipped forever.
    rows = (
        await session.execute(
            select(DeliveryLog.kind).where(
                DeliveryLog.invoice_id == invoice_id,
                DeliveryLog.status == DeliveryStatus.sent,
            )
        )
    ).scalars().all()
    return {k.value for k in rows}


async def _msg(session: AsyncSession, key: str, inv: Invoice, reseller: Reseller) -> str:
    return await texts.render(
        session, key,
        name=reseller.name, period=inv.period_label,
        amount_toman=f"{float(inv.amount_toman):,.0f}",
        amount_usdt=f"{float(inv.amount_usdt):,.2f}",
    )


async def run_dunning(session: AsyncSession, *, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date()

    cfg = await settings_service.get_many(
        session,
        ["reminder1_day", "reminder2_day", "warning_day", "enforcement_day", "enforcement_enabled"],
    )
    d1 = int(cfg.get("reminder1_day") or 2)
    d2 = int(cfg.get("reminder2_day") or 4)
    dw = int(cfg.get("warning_day") or 5)
    de = int(cfg.get("enforcement_day") or 5)

    invoices = (
        await session.execute(
            select(Invoice).where(Invoice.status.in_(_ACTIVE), Invoice.sent_at.is_not(None))
        )
    ).scalars().all()

    # A pending payment means the customer paid and is waiting on the OWNER's review.
    # Don't punish them in the meantime: hold the whole reminder/warning/enforce cycle for
    # the invoice the payment is attached to, and never auto-suspend a reseller who has any
    # pending payment. The hold lifts as soon as the owner confirms (→ paid, leaves _ACTIVE)
    # or rejects (→ no longer pending, cycle resumes on the original timeline).
    pending_rows = (
        await session.execute(
            select(Payment.invoice_id, Payment.reseller_id)
            .where(Payment.status == PaymentStatus.pending)
        )
    ).all()
    held_invoice_ids = {iid for iid, _ in pending_rows if iid is not None}
    held_reseller_ids = {rid for _, rid in pending_rows if rid is not None}

    counts = {"reminder1": 0, "reminder2": 0, "warning": 0, "enforced": 0,
              "enforced_dry": 0, "deferred": 0, "on_hold": 0}
    enforced_links: list[str] = []  # clickable owner-facing links of enforced resellers
    bot: Bot | None = await build_bot(session)
    try:
        for inv in invoices:
            # Dunning anchor: a set payment deadline (deferred_until) RESTARTS the whole
            # reminder/enforcement cycle from that date — reminders at +d1/+d2, warning &
            # cutoff at +dw/+de days after the deadline. Otherwise count from sent_at.
            if inv.deferred_until:
                anchor = inv.deferred_until
            else:
                sent_at = inv.sent_at
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=dt.timezone.utc)
                anchor = sent_at.date()
            days = (today - anchor).days
            if days < 0:
                # Deadline still in the future → fully paused.
                counts["deferred"] += 1
                continue
            if inv.id in held_invoice_ids:
                # A payment for THIS invoice is awaiting the owner's confirm/reject.
                counts["on_hold"] += 1
                continue
            reseller = await session.get(Reseller, inv.reseller_id)
            if reseller is None:
                continue
            done = await _done_kinds(session, inv.id)

            if days >= d1 and DeliveryKind.reminder1.value not in done:
                await notifier.send_to_reseller(
                    session, reseller, await _msg(session, "tpl_reminder1", inv, reseller),
                    kind=DeliveryKind.reminder1, invoice_id=inv.id, bot=bot,
                )
                counts["reminder1"] += 1

            if days >= d2 and DeliveryKind.reminder2.value not in done:
                await notifier.send_to_reseller(
                    session, reseller, await _msg(session, "tpl_reminder2", inv, reseller),
                    kind=DeliveryKind.reminder2, invoice_id=inv.id, bot=bot,
                )
                counts["reminder2"] += 1

            if days >= dw and DeliveryKind.warning.value not in done:
                await notifier.send_to_reseller(
                    session, reseller, await _msg(session, "tpl_warning", inv, reseller),
                    kind=DeliveryKind.warning, invoice_id=inv.id, bot=bot,
                )
                if inv.status == InvoiceStatus.sent:
                    inv.status = InvoiceStatus.overdue
                    await session.commit()
                counts["warning"] += 1

            if (days >= de and reseller.enforcement_state == EnforcementState.active
                    and inv.reseller_id not in held_reseller_ids):
                # A live enforcement flips enforcement_state away from `active`, so it's
                # naturally skipped next run. A DRY-RUN doesn't change state, so without a
                # guard it would log a fresh EnforcementAction every single day. In
                # dry-run, log at most once per invoice; live failures still retry.
                if not bool(cfg.get("enforcement_enabled")):
                    already = (
                        await session.execute(
                            select(EnforcementAction.id)
                            .where(EnforcementAction.invoice_id == inv.id)
                            .limit(1)
                        )
                    ).first()
                    if already:
                        continue
                action = await enforcement.enforce_reseller(session, reseller, invoice_id=inv.id)
                if action.dry_run:
                    counts["enforced_dry"] += 1
                else:
                    inv.status = InvoiceStatus.enforced
                    await session.commit()
                    counts["enforced"] += 1
                    from app.services.owner_notify import user_link

                    enforced_links.append(user_link(reseller))
    finally:
        if bot is not None:
            await bot.session.close()

    log.info("Dunning run: %s", counts)
    return {"date": today.isoformat(), "enforced_resellers": enforced_links, **counts}
