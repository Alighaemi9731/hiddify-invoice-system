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


class PaymentActionResult(BaseModel):
    status: str
    paid: bool
    message: str
