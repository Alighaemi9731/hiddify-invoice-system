from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class CrmMonthPoint(BaseModel):
    """One month of a reseller's history. `label` is the ASCII billing period, `YYYY-MM`."""

    label: str
    gb: float
    services: int
    amount_toman: float


class CrmBoardRow(BaseModel):
    reseller_id: int
    reseller_name: str
    admin_uuid: str
    panel_id: int
    panel_key: str
    segment: str
    sub_resellers: int
    registered: bool

    value_at_risk_toman: float
    mtd_services: int
    mtd_gb: float
    projected_gb: float
    avg_prev_gb: float
    last_sale_date: dt.date | None
    days_since_last_sale: int | None
    account_age_days: int

    outstanding_toman: float
    outstanding_count: int
    oldest_unpaid_period: str | None

    # Follow-up state (null when this reseller has never been touched).
    last_touch_at: dt.datetime | None
    touch_count: int
    snoozed_until: dt.date | None
    muted: bool
    note: str
    due: bool

    # Last 6 months, oldest first — the inline trend sparkline.
    trend_gb: list[float]


class CrmSummary(BaseModel):
    """Per-segment counts over the WHOLE eligible population plus the size of the work queue."""

    counts: dict[str, int]
    total: int
    due: int
    snoozed: int
    muted: int
    # The owner's `crm_snooze_default_days`, so the follow-up dialog pre-selects their setting
    # instead of hardcoding a number the settings page claims to control.
    snooze_default_days: int
    generated_at: dt.datetime


class CrmFollowupRow(BaseModel):
    id: int
    reseller_id: int | None
    reseller_name: str
    reseller_admin_uuid: str
    panel_key: str
    segment: str
    note: str
    snoozed_until: dt.date | None
    muted: bool
    actor: str
    created_at: dt.datetime


class CrmResellerDetail(BaseModel):
    row: CrmBoardRow
    months: list[CrmMonthPoint]
    followups: list[CrmFollowupRow]


class FollowupBody(BaseModel):
    """Log one follow-up. `snooze_days` wins over `snooze_until` when both are given;
    both absent means the owner's `crm_snooze_default_days`, and an explicit 0 means
    "no snooze — keep it on the list"."""

    note: str = Field(default="", max_length=2000)
    snooze_days: int | None = Field(default=None, ge=0, le=365)
    snooze_until: dt.date | None = None
    muted: bool = False
    # Replaces the pinned per-reseller note when provided; omit to leave it untouched.
    pinned_note: str | None = Field(default=None, max_length=2000)


class BulkFollowupBody(FollowupBody):
    reseller_ids: list[int] = Field(min_length=1, max_length=500)


class FollowupResult(BaseModel):
    updated: int
    snoozed_until: dt.date | None
    muted: bool
