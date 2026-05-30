from __future__ import annotations

import datetime as dt

from pydantic import BaseModel


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
    last_seen_at: dt.datetime | None


class ResellerUpdate(BaseModel):
    price_per_gb: int | None = None
    min_sale_toman: int | None = None
    exclude_from_billing: bool | None = None
