from __future__ import annotations

import datetime as dt

from pydantic import BaseModel


class PaymentOut(BaseModel):
    id: int
    reseller_id: int
    reseller_name: str | None
    invoice_id: int | None
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


class ConfirmPaymentBody(BaseModel):
    # The exact invoices this payment covers (owner-selected). None → settle the single
    # invoice the payment was linked to (backward compatible).
    invoice_ids: list[int] | None = None


class DueInvoiceOut(BaseModel):
    id: int
    period_label: str
    reseller_name: str
    panel_key: str
    amount_usdt: float
    amount_toman: float
    status: str


class PaymentActionResult(BaseModel):
    status: str
    paid: bool
    message: str
