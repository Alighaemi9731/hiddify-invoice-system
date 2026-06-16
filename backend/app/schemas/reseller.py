from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class ResellerOut(BaseModel):
    id: int
    panel_id: int
    panel_key: str
    admin_uuid: str
    name: str
    parent_admin_uuid: str | None
    mode: str
    is_owner: bool
    comment: str | None
    exclude_from_billing: bool
    price_per_gb: int | None
    effective_price_per_gb: int
    min_sale_toman: int | None
    bot_chat_id: int | None
    username: str | None = None   # Telegram @username (when known) → deep-link to the admin's PV
    panel_telegram_id: int | None
    link_tag: str | None
    registered: bool
    enforcement_state: str
    panel_max_users: int | None
    panel_max_active_users: int | None
    can_add_admin: bool = False
    users_count: int = 0          # users this admin created (panel snapshot)
    active_users_count: int = 0   # of those, currently enabled+active
    capacity_pct: float = 0       # users_count / max_users * 100 (0 when no limit)
    last_seen_at: dt.datetime | None


class AbsentResellerOut(BaseModel):
    """A reseller row whose admin was removed from the Hiddify panel (last_seen_at older than the
    panel's latest successful sync) but whose DB row still lingers — a candidate for safe deletion."""
    id: int
    panel_id: int
    panel_key: str
    admin_uuid: str
    name: str
    last_seen_at: dt.datetime | None
    users_count: int = 0
    sub_resellers: int = 0           # direct children still lingering in the DB
    has_nondraft_invoices: bool = False  # delivered/paid invoices exist → louder delete warning
    has_payments: bool = False


class ResellerUpdate(BaseModel):
    price_per_gb: int | None = Field(default=None, ge=0)
    min_sale_toman: int | None = Field(default=None, ge=0)
    exclude_from_billing: bool | None = None


class BumpLimitsBody(BaseModel):
    amount: int = Field(default=100, ge=1, le=1_000_000)


class CanAddAdminBody(BaseModel):
    enabled: bool
