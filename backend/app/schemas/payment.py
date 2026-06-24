from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class InvoiceBrief(BaseModel):
    """One invoice covered by a payment — a payment may settle several at once."""

    id: int
    period: str | None = None
    amount_toman: float = 0
    equiv: str = ""   # pre-formatted crypto equivalent in the paid currency («30.86 USDT»)


class PaymentOut(BaseModel):
    id: int
    number: str = ""   # public 8-digit tracking number (non-sequential; hides the count)
    reseller_id: int
    reseller_name: str | None
    reseller_chat_id: int | None = None   # Telegram chat id → deep-link to the customer's PV
    reseller_username: str | None = None
    invoice_id: int | None   # the PRIMARY/first invoice (back-compat single-invoice views)
    invoice_period: str | None = None   # the period of the primary invoice (back-compat)
    invoice_amount_toman: float = 0     # the primary invoice amount (back-compat)
    # Pre-formatted crypto equivalent of the invoice in the PAID currency («30.86 USDT» /
    # «20.06 TON») — so the panel never shows a 0.00 from the (unverified) payment.amount_usdt.
    invoice_equiv: str = ""
    # The full set this payment covers (>=1). A multi-invoice payment lists every covered
    # invoice; `total_amount_toman` is their sum (what the owner confirms/rejects together).
    invoices: list[InvoiceBrief] = Field(default_factory=list)
    invoice_ids: list[int] = Field(default_factory=list)
    invoice_count: int = 1
    total_amount_toman: float = 0
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


class PaymentActionResult(BaseModel):
    status: str
    paid: bool
    message: str
