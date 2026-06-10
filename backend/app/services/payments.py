"""Payment confirmation and the optional BEP-20 USDT chain check.

The reseller submits proof for one explicitly selected invoice. The owner makes the final
confirm/reject decision; for USDT/BSC they may first ask this service to verify destination,
token contract, amount, and confirmations through BscScan. A confirmed payment marks only its
linked invoice paid and restores access only when no other due debt remains.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
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


async def _reseller_has_other_due(
    session: AsyncSession, reseller_id: int, exclude_invoice_id: int | None
) -> bool:
    """True if the reseller still has another OWED, non-deferred invoice. Used to avoid
    restoring a suspended reseller while they still owe on a different invoice — paying one
    invoice must not lift enforcement when other debts remain."""
    today = dt.date.today()
    rows = (
        await session.execute(
            select(Invoice).where(
                Invoice.reseller_id == reseller_id, Invoice.status.in_(_OWED)
            )
        )
    ).scalars().all()
    for inv in rows:
        if exclude_invoice_id is not None and inv.id == exclude_invoice_id:
            continue
        if inv.deferred_until and inv.deferred_until > today:
            continue  # deadline in the future → not currently due
        return True
    return False


async def _maybe_restore(
    session: AsyncSession, reseller: Reseller | None, *, exclude_invoice_id: int | None = None
) -> None:
    if reseller is None:
        return
    if not await settings_service.get(session, "auto_restore_on_payment", True):
        return
    # Only lift enforcement when NO other due (non-deferred) invoice remains for this reseller.
    if await _reseller_has_other_due(session, reseller.id, exclude_invoice_id):
        log.info("restore held for reseller %s: other due invoice(s) remain", reseller.id)
        return
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
    params: dict[str, str | int] = {
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

    # On-chain verify is BSC/USDT-only. A non-BSC (TON) hash must never be looked up on BscScan —
    # it would never be found and the message would be misleading. Hold for manual review.
    if payment.chain and payment.chain not in ("bsc", ""):
        return PaymentResult(
            "pending", False,
            "بررسی خودکار فقط برای USDT است؛ این پرداخت را به‌صورت دستی بررسی و تأیید کنید.",
        )

    invoice = await session.get(Invoice, payment.invoice_id) if payment.invoice_id else None

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
        api_url = str(cfg.get("bscscan_api_url") or "")
        if not payment.txid:
            return PaymentResult("rejected", False, "شناسه تراکنش ثبت نشده است.")
        check = await _bscscan_tokentx(api_url, api_key, wallet, contract, payment.txid)
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
                             "✅ پرداخت دریافت شد؛ بدهی فعالی برای این فاکتور نبود."
                             + _ref_line(payment.id))

    target_amt = Decimal(str(target.amount_usdt or 0))
    # Safety net: never AUTO-confirm a zero-amount invoice. If the conversion rate was 0 when
    # the invoice was generated (e.g. auto mode before a live rate was fetched), amount_usdt is
    # 0 and the "amount too low" floor below (anything < 0) could never fire — so a dust
    # transfer would clear the whole Toman invoice. Hold it for the owner's manual review.
    if target_amt <= 0:
        payment.status = PaymentStatus.pending
        if "[needs manual review: zero invoice amount]" not in (payment.note or ""):
            payment.note = (payment.note or "") + " [needs manual review: zero invoice amount]"
        await session.commit()
        return PaymentResult(
            "pending", False,
            "مبلغ این فاکتور نامشخص است؛ پرداخت برای بررسیِ دستی ثبت شد.",
        )
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
    await _maybe_restore(
        session, await session.get(Reseller, target.reseller_id), exclude_invoice_id=target.id
    )

    msg = await _payment_received_text(session, target.period_label, payment.id)
    if notify_reseller:
        r = await session.get(Reseller, target.reseller_id)
        if r is not None:
            await notifier.send_to_reseller(session, r, msg, kind=DeliveryKind.payment_ack)
    return PaymentResult("confirmed", True, msg)


def _ref_line(code: int | None) -> str:
    """Tracking-number footer so the customer can quote «شمارهٔ پیگیری #N» to support and the
    owner can find that exact payment in the panel."""
    return f"\n🔖 شمارهٔ پیگیری: #{code}" if code else ""


async def _payment_received_text(session: AsyncSession, period: str, code: int | None = None) -> str:
    from app.bot import texts

    # «—» when there's no linked invoice, so the template never renders «فاکتور دوره  …» (a
    # dangling double space).
    period = (period or "").strip() or "—"
    return await texts.render(session, "tpl_payment_received", period=period) + _ref_line(code)


async def _payment_rejected_text(session: AsyncSession, period: str, code: int | None = None) -> str:
    from app.bot import texts

    period = (period or "").strip() or "—"
    return await texts.render(session, "tpl_payment_rejected", period=period) + _ref_line(code)


def _settled_ids(payment: Payment) -> list[int]:
    """The invoice ids a payment has settled (from settled_invoice_ids, else invoice_id)."""
    if payment.settled_invoice_ids:
        return [int(x) for x in payment.settled_invoice_ids.split(",") if x.strip().isdigit()]
    return [payment.invoice_id] if payment.invoice_id else []


async def _settled_by_other_confirmed(
    session: AsyncSession, invoice_id: int, exclude_payment_id: int
) -> bool:
    """True if a DIFFERENT confirmed payment also settled this invoice. Reversing/deleting one
    payment must not un-pay an invoice that another confirmed payment still settles — otherwise
    rejecting a duplicate/mis-clicked payment would wrongly mark a genuinely-paid invoice owed."""
    others = (
        await session.execute(
            select(Payment).where(
                Payment.id != exclude_payment_id, Payment.status == PaymentStatus.confirmed
            )
        )
    ).scalars().all()
    return any(invoice_id in _settled_ids(p) for p in others)


async def _revert_settled_invoices(
    session: AsyncSession, payment: Payment
) -> None:
    """Revert the invoices a (confirmed) payment settled back to owed — UNLESS another
    confirmed payment still settles them. Reverted invoices get a fresh dunning cycle and the
    stale txid cleared from the ledger."""
    from app.services import dunning

    ids = _settled_ids(payment)
    if not ids:
        return
    rows = (
        await session.execute(select(Invoice).where(Invoice.id.in_(ids)))
    ).scalars().all()
    for inv in rows:
        if inv.status != InvoiceStatus.paid:
            continue
        if await _settled_by_other_confirmed(session, inv.id, payment.id):
            continue  # still settled elsewhere — leave it paid
        inv.status = InvoiceStatus.sent
        inv.paid_at = None
        await dunning.reset_cycle(session, inv, restamp_sent_at=True)
        r = await session.get(Reseller, inv.reseller_id)
        # record() clears the stale txid because the invoice is no longer paid.
        await financial_archive.record(session, inv, reseller=r)


async def _mark_invoices_paid(
    session: AsyncSession, invoices: list[Invoice], payment: Payment
) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    for inv in invoices:
        # Only an OWED invoice may be marked paid. Guarding here (not just in the caller)
        # protects EVERY settlement path: confirming a stale payment whose linked invoice was
        # meanwhile reverted to draft / canceled / already paid must NOT resurrect it as paid
        # or write a duplicate ledger row. (verify_payment also guards before calling.)
        if inv.status not in _OWED:
            continue
        inv.status = InvoiceStatus.paid
        inv.paid_at = now
        reseller = await session.get(Reseller, inv.reseller_id)
        await financial_archive.record(session, inv, reseller=reseller, txid=payment.txid)


async def confirm_manually(session: AsyncSession, payment_id: int) -> PaymentResult:
    """Owner override: mark a payment confirmed (without on-chain verification) for the SINGLE
    invoice it's linked to — payments are per-invoice, so there's nothing to choose.

    Reversible: works on a previously rejected payment too (recovers a mis-click). The reseller
    is notified only when the status actually CHANGES to confirmed (re-confirming an already-
    confirmed payment is silent), so a double-click doesn't spam them.
    """
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None:
        return PaymentResult("rejected", False, "Payment not found")
    was_confirmed = payment.status == PaymentStatus.confirmed
    reseller = await session.get(Reseller, payment.reseller_id)
    inv = await session.get(Invoice, payment.invoice_id) if payment.invoice_id else None
    # Don't "confirm" a payment whose invoice can't actually be settled (it was reverted to
    # draft or canceled) — that would leave the payment marked confirmed while the invoice stays
    # unpaid, misleading the owner. Tell them to fix the invoice first; leave the payment pending.
    if inv is not None and inv.status in (InvoiceStatus.draft, InvoiceStatus.canceled):
        return PaymentResult(
            "pending", False,
            "فاکتورِ مرتبط در وضعیتِ پیش‌نویس/لغوشده است؛ ابتدا آن را صادر یا اصلاح کنید.",
        )
    targets = [inv] if inv is not None else []

    await _mark_invoices_paid(session, targets, payment)
    payment.status = PaymentStatus.confirmed
    payment.verified_at = dt.datetime.now(dt.timezone.utc)
    if inv is not None:
        payment.settled_invoice_ids = str(inv.id)
        if not payment.amount_usdt:
            payment.amount_usdt = float(inv.amount_usdt or 0)
    if "[manually confirmed]" not in (payment.note or ""):
        payment.note = (payment.note or "") + " [manually confirmed]"
    await session.commit()

    if inv is not None:
        await _maybe_restore(
            session, await session.get(Reseller, inv.reseller_id), exclude_invoice_id=inv.id
        )

    if reseller is not None and not was_confirmed:
        period = inv.period_label if inv is not None else ""
        await notifier.send_to_reseller(
            session, reseller, await _payment_received_text(session, period, payment.id),
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
        await _revert_settled_invoices(session, payment)
    await session.commit()
    # Tell the customer their payment wasn't accepted — but only on a real state change
    # (so toggling reject→confirm→reject, or a double-click, doesn't spam them).
    if reseller is not None and not was_rejected:
        period = invoice.period_label if invoice else ""
        await notifier.send_to_reseller(
            session, reseller, await _payment_rejected_text(session, period, payment.id),
            kind=DeliveryKind.payment_ack, invoice_id=payment.invoice_id,
        )
    return PaymentResult("rejected", False, "Rejected")


async def delete_payment(session: AsyncSession, payment_id: int) -> bool:
    """Delete a payment row entirely (e.g. to clean up test data).

    If the payment had CONFIRMED an invoice, that invoice is first reverted to owed (and the
    ledger updated) so we never leave a 'paid' invoice with no payment behind it. The proof
    image file, if any, is removed too. Returns False if the payment doesn't exist.
    """
    payment = await session.get(Payment, payment_id, with_for_update=True)
    if payment is None:
        return False
    if payment.status == PaymentStatus.confirmed:
        await _revert_settled_invoices(session, payment)
    if payment.proof_path and os.path.exists(payment.proof_path):
        try:
            os.remove(payment.proof_path)
        except OSError:
            log.warning("failed to remove proof file %s", payment.proof_path, exc_info=True)
    await session.delete(payment)
    await session.commit()
    return True
