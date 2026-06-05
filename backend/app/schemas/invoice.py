from __future__ import annotations

import datetime as dt

from pydantic import BaseModel


class InvoiceLineOut(BaseModel):
    end_user_uuid: str
    name: str
    start_date: dt.date | None
    usage_gb: float
    added_by_uuid: str | None
    sub_reseller_name: str = ""


class InvoiceOut(BaseModel):
    id: int
    reseller_id: int
    reseller_name: str
    panel_id: int
    panel_key: str
    period_label: str
    period_start: dt.date
    period_end: dt.date
    usage_gb: float
    users_count: int
    price_per_gb: int
    amount_toman: float
    base_amount_toman: float = 0
    min_sale_toman: int = 0
    floor_applied: bool = False
    status: str
    sent_at: dt.datetime | None
    paid_at: dt.datetime | None
    deferred_until: dt.date | None = None
    defer_note: str | None = None
    created_at: dt.datetime | None


class InvoiceDetail(InvoiceOut):
    lines: list[InvoiceLineOut] = []


class InvoiceEdit(BaseModel):
    usage_gb: float | None = None
    price_per_gb: int | None = None
    amount_toman: float | None = None  # if set, overrides usage*price


class InvoiceDefer(BaseModel):
    deferred_until: dt.date | None = None  # null clears the deferral
    defer_note: str | None = None


class GenerateRequest(BaseModel):
    period: str  # "YYYY-MM"
    panel_id: int | None = None
    force: bool = False


class GenerateResult(BaseModel):
    period: str
    created: int
    updated: int
    skipped_existing: int
    zero_skipped: int
    total_amount_toman: float
    invoice_ids: list[int]
