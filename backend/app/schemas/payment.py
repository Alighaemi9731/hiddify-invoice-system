from __future__ import annotations

import datetime as dt

from pydantic import BaseModel


class PaymentOut(BaseModel):
    id: int
    reseller_id: int
    reseller_name: str | None
    reseller_chat_id: int | None = None   # Telegram chat id → deep-link to the customer's PV
    reseller_username: str | None = None
    invoice_id: int | None
    invoice_period: str | None = None   # the period of the single invoice this payment is for
    invoice_amount_toman: float = 0
    # Pre-formatted crypto equivalent of the invoice in the PAID currency («30.86 USDT» /
    # «20.06 TON») — so the panel never shows a 0.00 from the (unverified) payment.amount_usdt.
    invoice_equiv: str = ""
    method: str
    status: str
    chain: str
    txid: str | None
    from_address: str | None
    to_address: str | None
    amount_usdt: float
    confirmations: int
    verified_at: dt.datetime | None
    created_at: dt.datetime | None
    note: str | None
    has_proof: bool = False


class ManualPaymentCreate(BaseModel):
    invoice_id: int
    amount_usdt: float = 0
    note: str | None = None


class PaymentActionResult(BaseModel):
    status: str
    paid: bool
    message: str
