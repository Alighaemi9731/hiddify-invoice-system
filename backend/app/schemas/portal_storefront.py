"""Explicit response models for the reseller storefront portal.

The storefront ORM contains bot and panel credentials.  Portal endpoints deliberately build these
DTOs field-by-field instead of enabling ORM serialization, so adding a model column cannot expose a
secret by accident.
"""
from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field

HealthErrorClass = Literal["unauthorized", "network", "configuration", "unknown"]


class StorefrontResellerOut(BaseModel):
    id: int
    name: str


class StorefrontPanelOut(BaseModel):
    id: int
    key: str


class StorefrontSummaryOut(BaseModel):
    id: int
    reseller: StorefrontResellerOut
    panel: StorefrontPanelOut
    bot_username: str | None
    enabled: bool
    status: str
    health_error_class: HealthErrorClass | None
    health_state_updated_at: dt.datetime | None
    shop_closed: bool
    role: Literal["owner"] = "owner"


class SalesBucketOut(BaseModel):
    count: int = 0
    amount_toman: int = 0


class SalesPeriodOut(BaseModel):
    gross_sales_toman: int = 0
    reversals_toman: int = 0
    net_sales_toman: int = 0
    purchase: SalesBucketOut = Field(default_factory=SalesBucketOut)
    renewal: SalesBucketOut = Field(default_factory=SalesBucketOut)
    unknown: SalesBucketOut = Field(default_factory=SalesBucketOut)


class DashboardRangeOut(BaseModel):
    from_date: dt.date
    to_date: dt.date
    timezone: Literal["Asia/Tehran"] = "Asia/Tehran"


class CustomerMetricsOut(BaseModel):
    total: int = 0
    active_30d: int = 0
    wallet_liability_toman: int = 0


class PendingTopupsOut(BaseModel):
    count: int = 0
    amount_toman: int = 0


class CreditMetricsOut(BaseModel):
    redemptions: int = 0
    bonus_toman: int = 0


class TrialConversionOut(BaseModel):
    trial_customers: int = 0
    converted_customers: int = 0
    rate: float | None = None


class StorefrontDashboardOut(BaseModel):
    storefront_id: int
    range: DashboardRangeOut
    sales_today: SalesPeriodOut
    sales_month: SalesPeriodOut
    sales_range: SalesPeriodOut
    customers: CustomerMetricsOut
    service_states: dict[str, int]
    near_expiry: int = 0
    pending_topups: PendingTopupsOut
    credits: CreditMetricsOut
    operation_states: dict[str, int]
    trial_conversion: TrialConversionOut


class BotHealthOut(BaseModel):
    enabled: bool
    status: str
    error_class: HealthErrorClass | None
    state_updated_at: dt.datetime | None


class PanelHealthOut(BaseModel):
    id: int
    key: str
    enabled: bool
    status: str
    last_synced_at: dt.datetime | None
    error_class: HealthErrorClass | None
    state_updated_at: dt.datetime | None


class StorefrontHealthOut(BaseModel):
    storefront_id: int
    bot: BotHealthOut
    panel: PanelHealthOut
    operation_states: dict[str, int]
