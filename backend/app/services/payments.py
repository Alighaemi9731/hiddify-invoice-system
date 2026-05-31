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
            )
    return _ChainCheck(False, None, None, Decimal(0), 0, error="transaction not found for this wallet")


async def verify_payment(session: AsyncSession, payment_id: int) -> PaymentResult:
    """Verify a pending TXID payment on-chain and apply it. Idempotent-ish."""
    payment = await session.get(Payment, payment_id)
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

    if not api_key or not wallet:
        # Can't auto-verify; leave pending for manual confirmation by the owner.
        return PaymentResult(
            "pending", False,
            "✅ شناسه تراکنش دریافت شد و پس از بررسی توسط پشتیبانی تأیید می‌شود.",
            detail="bscscan api key or wallet not configured",
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

    if check.confirmations < min_conf:
        payment.status = PaymentStatus.pending
        await session.commit()
        return PaymentResult("pending", False,
                             f"تراکنش یافت شد اما هنوز تأییدیه کافی ندارد ({check.confirmations}/{min_conf}).")

    expected = Decimal(str(invoice.amount_usdt)) if invoice else Decimal(0)
    if expected > 0 and check.amount_usdt + tolerance < expected:
        payment.status = PaymentStatus.rejected
        payment.note = f"amount too low: {check.amount_usdt} < {expected}"
        await session.commit()
        return PaymentResult(
            "rejected", False,
            f"❌ مبلغ واریزی ({check.amount_usdt:.2f} USDT) کمتر از مبلغ فاکتور ({expected:.2f} USDT) است.",
        )

    # Success.
    await _apply_confirmed(session, payment, invoice, reseller)
    period = invoice.period_label if invoice else ""
    msg = await _payment_received_text(session, period)
    return PaymentResult("confirmed", True, msg)


async def _payment_received_text(session: AsyncSession, period: str) -> str:
    from app.bot import texts

    return await texts.render(session, "tpl_payment_received", period=period)


async def _apply_confirmed(
    session: AsyncSession, payment: Payment, invoice: Invoice | None, reseller: Reseller | None
) -> None:
    payment.status = PaymentStatus.confirmed
    payment.verified_at = dt.datetime.now(dt.timezone.utc)
    if invoice is not None:
        invoice.status = InvoiceStatus.paid
        invoice.paid_at = dt.datetime.now(dt.timezone.utc)
        await financial_archive.record(session, invoice, reseller=reseller, txid=payment.txid)
    await session.commit()

    # Auto-restore panel access if the reseller had been enforced.
    if reseller is not None:
        auto = await settings_service.get(session, "auto_restore_on_payment", True)
        if auto:
            try:
                from app.services import enforcement  # M6

                await enforcement.restore_reseller(session, reseller)
            except Exception:  # noqa: BLE001 — enforcement module/credentials may be absent
                log.info("restore skipped/failed for reseller %s", reseller.id)


async def confirm_manually(session: AsyncSession, payment_id: int) -> PaymentResult:
    """Owner override: mark a payment confirmed without on-chain verification."""
    payment = await session.get(Payment, payment_id)
    if payment is None:
        return PaymentResult("rejected", False, "Payment not found")
    invoice = await session.get(Invoice, payment.invoice_id) if payment.invoice_id else None
    reseller = await session.get(Reseller, payment.reseller_id)
    payment.note = (payment.note or "") + " [manually confirmed]"
    await _apply_confirmed(session, payment, invoice, reseller)
    if reseller is not None:
        period = invoice.period_label if invoice else ""
        await notifier.send_to_reseller(
            session, reseller, await _payment_received_text(session, period),
            kind=DeliveryKind.payment_ack, invoice_id=payment.invoice_id,
        )
    return PaymentResult("confirmed", True, "Confirmed")
