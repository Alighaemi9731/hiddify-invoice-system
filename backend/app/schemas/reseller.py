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


class ResellerUpdate(BaseModel):
    price_per_gb: int | None = Field(default=None, ge=0)
    min_sale_toman: int | None = Field(default=None, ge=0)
    exclude_from_billing: bool | None = None


class BumpLimitsBody(BaseModel):
    amount: int = Field(default=100, ge=1, le=1_000_000)


class CanAddAdminBody(BaseModel):
    enabled: bool
