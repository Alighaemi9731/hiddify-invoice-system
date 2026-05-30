from __future__ import annotations

import datetime as dt

from pydantic import BaseModel


class PanelSalesRow(BaseModel):
    panel_id: int
    panel_key: str
    invoices: int
    usage_gb: float
    amount_toman: float
    amount_usdt: float


class SalesRow(BaseModel):
    invoice_id: int
    reseller_id: int
    reseller_name: str
    panel_key: str
    usage_gb: float
    amount_toman: float
    amount_usdt: float
    status: str


class DebtRow(BaseModel):
    reseller_id: int
    reseller_name: str
    panel_key: str
    bot_registered: bool
    invoices_count: int
    outstanding_toman: float
    outstanding_usdt: float
    oldest_period: str | None


class StatusCount(BaseModel):
    status: str
    count: int


class DeliveryLogRow(BaseModel):
    id: int
    reseller_id: int | None
    reseller_name: str | None
    invoice_id: int | None
    kind: str
    status: str
    error: str | None
    created_at: dt.datetime | None


class EnforcementActionRow(BaseModel):
    id: int
    reseller_id: int
    reseller_name: str | None
    invoice_id: int | None
    action: str
    status: str
    dry_run: bool
    affected_count: int
    error: str | None
    created_at: dt.datetime | None


class DashboardSummary(BaseModel):
    period: str
    panels: int
    resellers: int
    billable_resellers: int
    registered_resellers: int
    invoices_total: int
    period_billed_toman: float
    period_billed_usdt: float
    period_paid_toman: float
    outstanding_toman: float
    outstanding_usdt: float
    status_counts: list[StatusCount]
    sales_by_panel: list[PanelSalesRow]
    top_resellers: list[SalesRow]
