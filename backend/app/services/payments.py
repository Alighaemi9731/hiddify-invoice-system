"""
BEP-20 USDT payment verification.

MVP flow: the reseller submits a TXID; we verify on-chain via the BscScan API that
a USDT (BEP-20) transfer to our wallet, of at least the invoice amount, with enough
confirmations, exists. On success the invoice is marked paid and (if the reseller was
enforced) access is auto-restored.

The module is structured so the later upgrade — per-reseller HD-wallet deposit
addresses + automatic monitoring — can replace the TXID step without touching the
confirm/restore logic.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from dataclasses import dataclass
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invoice, Payment, Reseller
from app.models.enums import (
    DeliveryKind,
    InvoiceStatus,
    PaymentStatus,
)
from app.services import financial_archive, notifier, settings_service

log = logging.getLogger("payments")

USDT_DECIMALS = 18

# Owed = delivered but not yet paid.
_OWED = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)


async def _chat_reseller_ids(session: AsyncSession, reseller: Reseller | None) -> list[int]:
    """All reseller rows that belong to the same Telegram user (one customer can be an
    admin on several panels). Used so one payment can clear debt across all of them."""
    if reseller is None:
        return []
    if reseller.bot_chat_id is None:
        return [reseller.id]
    rows = (
        await session.execute(
            select(Reseller.id).where(Reseller.bot_chat_id == reseller.bot_chat_id)
        )
    ).scalars().all()
    return list(rows) or [reseller.id]


async def _due_now_invoices(session: AsyncSession, reseller_ids: list[int]) -> list[Invoice]:
    """Outstanding invoices that are due NOW (oldest first) — owed and NOT deferred to a
    future date. Deferred invoices wait until their deadline."""
    if not reseller_ids:
        return []
    today = dt.date.today()
    rows = (
        await session.execute(
            select(Invoice)
            .where(Invoice.reseller_id.in_(reseller_ids), Invoice.status.in_(_OWED))
            .order_by(Invoice.period_start.asc())
        )
    ).scalars().all()
    return [i for i in rows if not (i.deferred_until and i.deferred_until > today)]


async def _maybe_restore(session: AsyncSession, reseller: Reseller | None) -> None:
    if reseller is None:
        return
    if await settings_service.get(session, "auto_restore_on_payment", True):
        try:
            from app.services import enforcement

            await enforcement.restore_reseller(session, reseller)
        except Exception:  # noqa: BLE001 — enforcement module/credentials may be absent
            log.info("restore skipped/failed for reseller %s", reseller.id)


@dataclass
class PaymentResult:
    status: str             # confirmed | pending | rejected
    paid: bool
    message_fa: str
    detail: str = ""


@dataclass
class _ChainCheck:
    found: bool
    to_address: str | None
    from_address: str | None
    amount_usdt: Decimal
    confirmations: int
    error: str | None = None
    contract_address: str | None = None  # the token contract of the matched tx


async def _bscscan_tokentx(
    api_url: str, api_key: str, wallet: str, contract: str, txid: str
) -> _ChainCheck:
    """Look up the USDT token transfers for our wallet and find the matching tx."""
    params = {
        "module": "account",
        "action": "tokentx",
        "address": wallet,
        "contractaddress": contract,
        "page": 1,
        "offset": 100,
        "sort": "desc",
        "apikey": api_key,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(api_url, params=params)
        resp.raise_for_status()
        data = resp.json()
    if str(data.get("status")) != "1" and not isinstance(data.get("result"), list):
        return _ChainCheck(False, None, None, Decimal(0), 0, error=str(data.get("message") or data.get("result")))
    for tx in data.get("result", []):
        if (tx.get("hash") or "").lower() == txid.lower():
            raw = Decimal(str(tx.get("value", "0")))
            amount = raw / (Decimal(10) ** USDT_DECIMALS)
            return _ChainCheck(
                found=True,
                to_address=(tx.get("to") or "").lower(),
                from_address=(tx.get("from") or "").lower(),
                amount_usdt=amount,
                confirmations=int(tx.get("confirmations", 0) or 0),
                contract_address=(tx.get("contractAddress") or "").lower(),
            )
    return _ChainCheck(False, None, None, Decimal(0), 0, error="transaction not found for this wallet")


async def verify_payment(
    session: AsyncSession, payment_id: int, *, notify_reseller: bool = False
) -> PaymentResult:
    """Verify a pending TXID payment on-chain and apply it. Idempotent-ish.
    `notify_reseller=True` (panel-triggered) sends the confirmation to the reseller's
    Telegram; the bot path leaves it False because it answers the chat inline."""
    # Lock the row (Postgres) and re-check status under the lock so a concurrent
    # verify/confirm can't double-settle the same payment. No-op on SQLite (tests).
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None:
        return PaymentResult("rejected", False, "پرداخت یافت نشد.")
    if payment.status == PaymentStatus.confirmed:
        return PaymentResult("confirmed", True, "این پرداخت قبلاً تأیید شده است.")

    invoice = await session.get(Invoice, payment.invoice_id) if payment.invoice_id else None
    reseller = await session.get(Reseller, payment.reseller_id)

    cfg = await settings_service.get_many(
        session,
        ["bscscan_api_key", "bscscan_api_url", "usdt_bep20_address", "usdt_bep20_contract",
         "min_confirmations", "payment_amount_tolerance_usdt"],
    )
    api_key = cfg.get("bscscan_api_key") or ""
    wallet = (cfg.get("usdt_bep20_address") or "").lower()
    contract = cfg.get("usdt_bep20_contract") or ""
    min_conf = int(cfg.get("min_confirmations") or 0)
    tolerance = Decimal(str(cfg.get("payment_amount_tolerance_usdt") or 0))

    if not api_key or not wallet or not contract:
        # Can't safely auto-verify without all three (a blank token contract would let a
        # worthless-token transfer to our wallet pass as USDT) — leave pending for manual review.
        return PaymentResult(
            "pending", False,
            "✅ شناسه تراکنش دریافت شد و پس از بررسی توسط پشتیبانی تأیید می‌شود.",
            detail="bscscan api key, wallet, or USDT contract not configured",
        )

    try:
        check = await _bscscan_tokentx(cfg["bscscan_api_url"], api_key, wallet, contract, payment.txid)
    except Exception as exc:  # noqa: BLE001
        log.exception("on-chain lookup failed")
        return PaymentResult("pending", False,
                             "✅ شناسه تراکنش دریافت شد و در حال بررسی است.",
                             detail=f"lookup error: {exc}")

    payment.raw_json = json.dumps(check.__dict__, default=str)[:4000]

    if not check.found:
        payment.status = PaymentStatus.pending
        payment.note = check.error
        await session.commit()
        return PaymentResult("pending", False,
                             "تراکنش هنوز روی شبکه پیدا نشد. لطفاً چند دقیقه بعد دوباره تلاش کنید.",
                             detail=check.error or "")

    payment.from_address = check.from_address
    payment.to_address = check.to_address
    payment.amount_usdt = float(check.amount_usdt)
    payment.confirmations = check.confirmations

    if check.to_address != wallet:
        payment.status = PaymentStatus.rejected
        payment.note = "destination address mismatch"
        await session.commit()
        return PaymentResult("rejected", False, "❌ آدرس مقصد تراکنش با کیف پول ما مطابقت ندارد.")

    # The matched tx must be for the configured USDT token contract — otherwise a transfer of
    # some other (worthless) token to our wallet, with the same nominal value, would pass.
    if (check.contract_address or "") != contract.lower():
        payment.status = PaymentStatus.rejected
        payment.note = f"token contract mismatch: {check.contract_address}"
        await session.commit()
        return PaymentResult("rejected", False, "❌ توکن این تراکنش با USDT موردنظر مطابقت ندارد.")

    if check.confirmations < min_conf:
        payment.status = PaymentStatus.pending
        await session.commit()
        return PaymentResult("pending", False,
                             f"تراکنش یافت شد اما هنوز تأییدیه کافی ندارد ({check.confirmations}/{min_conf}).")

    # Settle ONLY the invoice this payment is for — payments are per-invoice now (no lumping
    # several invoices into one transfer), which keeps confirmation simple and unambiguous.
    target = await session.get(Invoice, payment.invoice_id) if payment.invoice_id else invoice
    if target is None or target.status not in _OWED:
        payment.status = PaymentStatus.confirmed
        payment.verified_at = dt.datetime.now(dt.timezone.utc)
        await session.commit()
        return PaymentResult("confirmed", True,
                             "✅ پرداخت دریافت شد؛ بدهی فعالی برای این فاکتور نبود.")

    target_amt = Decimal(str(target.amount_usdt or 0))
    if check.amount_usdt + tolerance < target_amt:
        payment.status = PaymentStatus.rejected
        payment.note = f"amount too low: {check.amount_usdt} < {target_amt}"
        await session.commit()
        return PaymentResult(
            "rejected", False,
            f"❌ مبلغ واریزی ({check.amount_usdt:.2f} USDT) کمتر از مبلغ این فاکتور "
            f"({target_amt:.2f} USDT) است.",
        )

    await _mark_invoices_paid(session, [target], payment)
    payment.status = PaymentStatus.confirmed
    payment.verified_at = dt.datetime.now(dt.timezone.utc)
    payment.invoice_id = target.id
    payment.settled_invoice_ids = str(target.id)
    await session.commit()
    await _maybe_restore(session, await session.get(Reseller, target.reseller_id))

    msg = await _payment_received_text(session, target.period_label)
    if notify_reseller:
        r = await session.get(Reseller, target.reseller_id)
        if r is not None:
            await notifier.send_to_reseller(session, r, msg, kind=DeliveryKind.payment_ack)
    return PaymentResult("confirmed", True, msg)


async def _payment_received_text(session: AsyncSession, period: str) -> str:
    from app.bot import texts

    return await texts.render(session, "tpl_payment_received", period=period)


async def _payment_rejected_text(session: AsyncSession, period: str) -> str:
    from app.bot import texts

    return await texts.render(session, "tpl_payment_rejected", period=period)


def _settled_ids(payment: Payment) -> list[int]:
    """The invoice ids a payment has settled (from settled_invoice_ids, else invoice_id)."""
    if payment.settled_invoice_ids:
        return [int(x) for x in payment.settled_invoice_ids.split(",") if x.strip().isdigit()]
    return [payment.invoice_id] if payment.invoice_id else []


async def _mark_invoices_paid(
    session: AsyncSession, invoices: list[Invoice], payment: Payment
) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    for inv in invoices:
        inv.status = InvoiceStatus.paid
        inv.paid_at = now
        reseller = await session.get(Reseller, inv.reseller_id)
        await financial_archive.record(session, inv, reseller=reseller, txid=payment.txid)


async def confirm_manually(
    session: AsyncSession, payment_id: int, invoice_ids: list[int] | None = None
) -> PaymentResult:
    """Owner override: mark a payment confirmed without on-chain verification.

    `invoice_ids` (optional): the exact set of the customer's OWED invoices this payment
    covers — so one transfer can settle several invoices, and the owner stays in control of
    which ones (vital for screenshots, where the amount isn't machine-readable). When None,
    falls back to settling the single invoice the payment was linked to (its oldest due).

    Reversible: works on a previously rejected payment too (recovers a mis-click). The
    reseller is notified only when the status actually CHANGES to confirmed (re-confirming
    an already-confirmed payment is silent), so a double-click doesn't spam them.
    """
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None:
        return PaymentResult("rejected", False, "Payment not found")
    was_confirmed = payment.status == PaymentStatus.confirmed
    reseller = await session.get(Reseller, payment.reseller_id)
    rids = set(await _chat_reseller_ids(session, reseller))

    if invoice_ids:
        rows = (
            await session.execute(select(Invoice).where(Invoice.id.in_(invoice_ids)))
        ).scalars().all()
        # Only the customer's own invoices that are owed (or already paid by THIS payment,
        # so a re-confirm after reject still works), oldest first.
        targets = [
            i for i in rows
            if i.reseller_id in rids and (i.status in _OWED or i.id in set(_settled_ids(payment)))
        ]
        targets.sort(key=lambda i: i.period_start)
    else:
        inv = await session.get(Invoice, payment.invoice_id) if payment.invoice_id else None
        targets = [inv] if inv is not None else []

    await _mark_invoices_paid(session, targets, payment)
    payment.status = PaymentStatus.confirmed
    payment.verified_at = dt.datetime.now(dt.timezone.utc)
    if targets:
        payment.invoice_id = targets[0].id
        payment.settled_invoice_ids = ",".join(str(i.id) for i in targets)
        if not payment.amount_usdt:
            payment.amount_usdt = float(sum(Decimal(str(i.amount_usdt or 0)) for i in targets))
    if "[manually confirmed]" not in (payment.note or ""):
        payment.note = (payment.note or "") + " [manually confirmed]"
    await session.commit()

    # Restore any reseller whose invoice we just settled (covers sub-rows of one customer).
    for rid in ({i.reseller_id for i in targets} or {payment.reseller_id}):
        await _maybe_restore(session, await session.get(Reseller, rid))

    if reseller is not None and not was_confirmed:
        periods = "، ".join(i.period_label for i in targets)
        await notifier.send_to_reseller(
            session, reseller, await _payment_received_text(session, periods),
            kind=DeliveryKind.payment_ack, invoice_id=payment.invoice_id,
        )
    return PaymentResult("confirmed", True, "Confirmed")


async def reject_payment(session: AsyncSession, payment_id: int) -> PaymentResult:
    """Owner rejects a payment. Reversible: if this payment had previously CONFIRMED one or
    more invoices (a mis-click, or a change of mind), EVERY invoice it settled is reverted to
    owed (unpaid) and the ledger updated, so the accounting stays consistent. The reseller is
    notified that their payment wasn't accepted — but only on a real state CHANGE to rejected
    (re-rejecting is silent), so toggling/double-clicks don't spam them. An already-enforced
    reseller is NOT re-suspended automatically — dunning re-escalates on its normal timeline,
    or the owner suspends manually."""
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None:
        return PaymentResult("rejected", False, "Payment not found")
    was_rejected = payment.status == PaymentStatus.rejected
    was_confirmed = payment.status == PaymentStatus.confirmed
    reseller = await session.get(Reseller, payment.reseller_id)
    invoice = await session.get(Invoice, payment.invoice_id) if payment.invoice_id else None
    payment.status = PaymentStatus.rejected
    payment.verified_at = None
    if was_confirmed:
        ids = _settled_ids(payment)
        if ids:
            rows = (
                await session.execute(select(Invoice).where(Invoice.id.in_(ids)))
            ).scalars().all()
            for inv in rows:
                if inv.status == InvoiceStatus.paid:
                    inv.status = InvoiceStatus.sent
                    inv.paid_at = None
                    r = await session.get(Reseller, inv.reseller_id)
                    await financial_archive.record(session, inv, reseller=r)
    await session.commit()
    # Tell the customer their payment wasn't accepted — but only on a real state change
    # (so toggling reject→confirm→reject, or a double-click, doesn't spam them).
    if reseller is not None and not was_rejected:
        period = invoice.period_label if invoice else ""
        await notifier.send_to_reseller(
            session, reseller, await _payment_rejected_text(session, period),
            kind=DeliveryKind.payment_ack, invoice_id=payment.invoice_id,
        )
    return PaymentResult("rejected", False, "Rejected")
