from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field


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
    lines: list[InvoiceLineOut] = Field(default_factory=list)


class InvoiceEdit(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    usage_gb: float | None = Field(default=None, ge=0)
    price_per_gb: int | None = Field(default=None, ge=0)
    amount_toman: float | None = Field(default=None, ge=0)  # overrides usage*price


class InvoiceDefer(BaseModel):
    deferred_until: dt.date | None = None  # null clears the deferral
    defer_note: str | None = None


class GenerateRequest(BaseModel):
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")  # "YYYY-MM"
    panel_id: int | None = Field(default=None, gt=0)
    force: bool = False


class GenerateResult(BaseModel):
    period: str
    created: int
    updated: int
    skipped_existing: int
    zero_skipped: int
    total_amount_toman: float
    invoice_ids: list[int] = Field(default_factory=list)
    skipped_panels: list[str] = Field(default_factory=list)
    reconciled_zero: int = 0
