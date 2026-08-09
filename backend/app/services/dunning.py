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
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import texts
from app.bot.telegram import build_bot
from app.models import DeliveryLog, EnforcementAction, Invoice, Payment, Reseller
from app.models.enums import (
    DeliveryKind,
    DeliveryStatus,
    EnforcementActionStatus,
    EnforcementState,
    InvoiceStatus,
    PaymentStatus,
)
from app.services import (
    enforcement,
    financial_archive,
    notifier,
    owner_notify,
    periods,
    settings_service,
)

log = logging.getLogger("dunning")

_ACTIVE = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)

_REMINDER_KINDS = [DeliveryKind.reminder1, DeliveryKind.reminder2, DeliveryKind.warning]


async def reset_cycle(
    session: AsyncSession, invoice: Invoice, *, restamp_sent_at: bool = False
) -> None:
    """Restart the dunning cycle for an invoice: clear its reminder/warning delivery marks
    so they re-fire. With `restamp_sent_at`, also re-anchor `sent_at` to now — used when a
    CONFIRMED payment is reversed (reject/delete/unmark), so the reseller gets a fresh
    reminder window instead of jumping straight back to overdue/enforcement on the next run.
    Does NOT commit (the caller's transaction does)."""
    await session.execute(
        delete(DeliveryLog).where(
            DeliveryLog.invoice_id == invoice.id,
            DeliveryLog.kind.in_(_REMINDER_KINDS),
        )
    )
    if restamp_sent_at and invoice.sent_at is not None:
        invoice.sent_at = dt.datetime.now(dt.timezone.utc)


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


async def _done_kinds_bulk(
    session: AsyncSession, invoice_ids: list[int]
) -> dict[int, set[str]]:
    """`_done_kinds` for a whole run in one grouped query.

    Same predicate, same semantics — only successfully-sent deliveries count. This used to be one
    query per invoice inside the dunning loop (~800–1200 round trips a day at 400 resellers).
    Small next to the Telegram sends that dominate the run, but free to fix. Chunked so a large
    debt backlog can't overflow SQLite's bound-parameter limit (same policy as
    metering._load_events / storefront_expiry._load_snaps).
    """
    out: dict[int, set[str]] = {i: set() for i in invoice_ids}
    if not invoice_ids:
        return out
    ids = sorted(invoice_ids)
    for i in range(0, len(ids), 500):
        rows = (
            await session.execute(
                select(DeliveryLog.invoice_id, DeliveryLog.kind).where(
                    DeliveryLog.invoice_id.in_(ids[i:i + 500]),
                    DeliveryLog.status == DeliveryStatus.sent,
                )
            )
        ).all()
        for invoice_id, kind in rows:
            out.setdefault(invoice_id, set()).add(kind.value)
    return out


async def _msg(session: AsyncSession, key: str, inv: Invoice, reseller: Reseller) -> str:
    return await texts.render(
        session, key,
        name=reseller.name, period=inv.period_label,
        amount_toman=f"{float(inv.amount_toman):,.0f}",
        amount_usdt=f"{float(inv.amount_usdt):,.2f}",
    )


# Advisory-lock key serializing whole dunning runs (the daily scheduler job vs the manual
# «اجرای یادآوری‌ها» endpoint). Adjacent to invoicing._BILLING_LOCK_KEY (…044) and
# enforcement._QUEUE_LOCK_KEY (…045).
_DUNNING_LOCK_KEY = 734_137_046


async def run_dunning(session: AsyncSession, *, now: dt.datetime | None = None) -> dict:
    """Serialize whole dunning runs so the daily scheduler job and a manual run can't overlap
    and double-send a reminder (each reads committed DeliveryLog before either commits its own).
    A transaction-level lock won't do — run_dunning commits mid-run — so hold a session-level
    advisory lock on a DEDICATED connection for the whole run; a second caller returns
    `{"skipped": "already_running"}`. No-op on SQLite (single-writer / tests)."""
    bind = session.bind
    if bind is None or getattr(bind.dialect, "name", "") != "postgresql":
        return await _run_dunning_impl(session, now=now)

    from sqlalchemy.ext.asyncio import async_sessionmaker

    lock_session = async_sessionmaker(bind, expire_on_commit=False)()
    try:
        got = (
            await lock_session.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": _DUNNING_LOCK_KEY}
            )
        ).scalar()
        if not got:
            return {"skipped": "already_running"}
        return await _run_dunning_impl(session, now=now)
    finally:
        try:
            await lock_session.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": _DUNNING_LOCK_KEY}
            )
        finally:
            await lock_session.close()


async def _run_dunning_impl(session: AsyncSession, *, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    # Day counting is TEHRAN-calendar based (like every deadline/eligibility check since B06).
    # Raw `.date()` here extracted the UTC day: a sent_at in the Tehran 00:00–03:29 window
    # anchored one day early, firing every reminder/enforcement threshold a day ahead.
    today = periods.to_local_date(now)

    cfg = await settings_service.get_many(
        session,
        ["reminder1_day", "reminder2_day", "warning_day", "enforcement_day",
         "enforcement_enabled", "pending_payment_hold_days"],
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

    # A pending payment means the customer paid and is waiting on the OWNER's review — don't
    # punish those invoices in the meantime. Scope is the payment's invoice SET: a pending proof
    # covering invoices {A,B} pauses dunning on BOTH A and B (a payment may cover several
    # invoices), but never the customer's unrelated debts (other invoices / other panels). And
    # the hold EXPIRES after `pending_payment_hold_days` so a stale, never-reviewed proof can't
    # shield a debt forever. The hold also lifts as soon as the owner confirms (→ paid, leaves
    # _ACTIVE) or rejects.
    hold_days = int(cfg.get("pending_payment_hold_days") or 7)
    cutoff = now - dt.timedelta(days=hold_days)

    def _aware(ts: dt.datetime | None) -> dt.datetime | None:
        if ts is not None and ts.tzinfo is None:
            return ts.replace(tzinfo=dt.timezone.utc)
        return ts

    from app.services.payments import _settled_ids

    pending_payments = (
        await session.execute(
            select(Payment).where(Payment.status == PaymentStatus.pending)
        )
    ).scalars().all()
    held_invoice_ids: set[int] = set()
    for p in pending_payments:
        if p.created_at is not None and (_aware(p.created_at) or cutoff) < cutoff:
            continue  # hold expired
        held_invoice_ids.update(_settled_ids(p))

    counts = {"reminder1": 0, "reminder2": 0, "warning": 0,
              "reminder1_sent": 0, "reminder2_sent": 0, "warning_sent": 0,
              "enforced": 0, "enforced_dry": 0, "enforcement_queued": 0,
              "deferred": 0, "on_hold": 0}
    enforced_links: list[str] = []  # clickable owner-facing links of enforced resellers
    # One grouped read of the delivery log for the whole run, instead of one query per invoice.
    # Safe to read up front: nothing in the loop below adds a `sent` DeliveryLog for an invoice
    # it has not yet reached, and each invoice's own `done` set is consulted exactly once.
    done_by_invoice = await _done_kinds_bulk(session, [inv.id for inv in invoices])
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
                if sent_at is None:
                    log.warning("dunning: invoice %s has owed status without sent_at", inv.id)
                    continue
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=dt.timezone.utc)
                anchor = periods.to_local_date(sent_at)
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
            done = done_by_invoice.get(inv.id, set())

            if days >= d1 and DeliveryKind.reminder1.value not in done:
                dl = await notifier.send_to_reseller(
                    session, reseller, await _msg(session, "tpl_reminder1", inv, reseller),
                    kind=DeliveryKind.reminder1, invoice_id=inv.id, bot=bot,
                )
                counts["reminder1"] += 1  # attempted
                if dl.status == DeliveryStatus.sent:
                    counts["reminder1_sent"] += 1

            if days >= d2 and DeliveryKind.reminder2.value not in done:
                dl = await notifier.send_to_reseller(
                    session, reseller, await _msg(session, "tpl_reminder2", inv, reseller),
                    kind=DeliveryKind.reminder2, invoice_id=inv.id, bot=bot,
                )
                counts["reminder2"] += 1
                if dl.status == DeliveryStatus.sent:
                    counts["reminder2_sent"] += 1

            if days >= dw and DeliveryKind.warning.value not in done:
                dl = await notifier.send_to_reseller(
                    session, reseller, await _msg(session, "tpl_warning", inv, reseller),
                    kind=DeliveryKind.warning, invoice_id=inv.id, bot=bot,
                )
                if inv.status == InvoiceStatus.sent:
                    inv.status = InvoiceStatus.overdue
                    # Mirror the status flip into the ledger so «تاریخچهٔ مالی» doesn't keep
                    # showing "sent" for an overdue invoice (money facts are unchanged).
                    await financial_archive.record(session, inv, reseller=reseller)
                    await session.commit()
                counts["warning"] += 1
                if dl.status == DeliveryStatus.sent:
                    counts["warning_sent"] += 1

            if days >= de and reseller.enforcement_state == EnforcementState.active:
                # A live enforcement flips enforcement_state away from `active`, so it's
                # naturally skipped next run. A DRY-RUN doesn't change state, so without a
                # guard it would log a fresh EnforcementAction every single day. In
                # dry-run, log at most once per invoice; live failures still retry.
                if not bool(cfg.get("enforcement_enabled")):
                    # Match only a DRY-RUN row for THIS invoice — the old check matched ANY
                    # action (incl. live/reverted rows from a past live cycle later disabled),
                    # so the dry-run intent would never be re-logged after such a cycle.
                    already = (
                        await session.execute(
                            select(EnforcementAction.id)
                            .where(
                                EnforcementAction.invoice_id == inv.id,
                                EnforcementAction.status == EnforcementActionStatus.dry_run,
                            )
                            .limit(1)
                        )
                    ).first()
                    if already:
                        continue
                action = await enforcement.queue_enforcement(session, reseller, invoice_id=inv.id)
                if action.dry_run:
                    counts["enforced_dry"] += 1
                else:
                    # A real (queued or done) suspension → give the owner a clickable link to the
                    # reseller's PV in the daily report so they can message them directly.
                    enforced_links.append(owner_notify.user_link(reseller))
                    if action.status.value == "done":
                        counts["enforced"] += 1
                    else:
                        counts["enforcement_queued"] += 1
    finally:
        if bot is not None:
            await bot.session.close()

    log.info("Dunning run: %s", counts)
    return {"date": today.isoformat(), "enforced_resellers": enforced_links, **counts}
