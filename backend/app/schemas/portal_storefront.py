"""Explicit response models for the reseller storefront portal.

The storefront ORM contains bot and panel credentials.  Portal endpoints deliberately build these
DTOs field-by-field instead of enabling ORM serialization, so adding a model column cannot expose a
secret by accident.
"""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


# Release B — audited plan/settings administration. These DTOs are deliberately explicit because
# StorefrontBot also carries an encrypted Telegram credential that must never enter a response.

class StorefrontMutationOut[T](BaseModel):
    result: T
    config_version: int = Field(ge=1)


class StorefrontPlanOut(BaseModel):
    id: int
    title: str = ""
    gb: int
    days: int
    price_toman: int
    enabled: bool
    sort_order: int


class StorefrontPlansOut(BaseModel):
    items: list[StorefrontPlanOut]
    config_version: int = Field(ge=1)


class StorefrontStrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StorefrontPlanCreate(StorefrontStrictBody):
    title: str = Field(default="", max_length=128)
    gb: int = Field(ge=1, le=100_000)
    days: int = Field(ge=1, le=3_650)
    price_toman: int = Field(ge=0, le=1_000_000_000_000)


class StorefrontPlanUpdate(StorefrontStrictBody):
    title: str | None = Field(default=None, max_length=128)
    gb: int | None = Field(default=None, ge=1, le=100_000)
    days: int | None = Field(default=None, ge=1, le=3_650)
    price_toman: int | None = Field(default=None, ge=0, le=1_000_000_000_000)

    @model_validator(mode="after")
    def require_change(self) -> StorefrontPlanUpdate:
        if not self.model_fields_set or all(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("at least one plan field is required")
        return self


class StorefrontEnabledBody(StorefrontStrictBody):
    enabled: bool


class StorefrontPlanOrderBody(StorefrontStrictBody):
    plan_ids: list[int] = Field(min_length=1, max_length=500)

    @field_validator("plan_ids")
    @classmethod
    def unique_ids(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value) or len(set(value)) != len(value):
            raise ValueError("plan_ids must contain unique positive ids")
        return value


class StorefrontPlanHistoryItemOut(BaseModel):
    id: int
    action: str
    actor_telegram_id: str | None
    actor_role: str
    source: str
    before: dict[str, Any] | list[Any] | None
    after: dict[str, Any] | list[Any] | None
    outcome: str
    created_at: dt.datetime


class StorefrontPlanHistoryOut(BaseModel):
    items: list[StorefrontPlanHistoryItemOut]


class StorefrontPaymentSettingsOut(BaseModel):
    pay_card_enabled: bool
    pay_usdt_enabled: bool
    pay_ton_enabled: bool
    card_number: str | None
    card_holder: str | None
    usdt_address: str | None
    ton_address: str | None


class StorefrontPaymentSettingsBody(StorefrontStrictBody):
    pay_card_enabled: bool | None = None
    pay_usdt_enabled: bool | None = None
    pay_ton_enabled: bool | None = None
    card_number: str | None = None
    card_holder: str | None = None
    usdt_address: str | None = None
    ton_address: str | None = None

    @field_validator("card_number")
    @classmethod
    def normalize_card(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed_separators = {" ", "-", "_", "\u200c"}
        if any(not ch.isdigit() and ch not in allowed_separators for ch in value):
            raise ValueError("card_number contains unsupported characters")
        digits = "".join(str(unicodedata.digit(ch)) for ch in value if ch.isdigit())
        if len(digits) != 16:
            raise ValueError("card_number must contain exactly 16 digits")
        return digits

    @field_validator("card_holder")
    @classmethod
    def validate_holder(cls, value: str | None) -> str | None:
        clean = value.strip() if value else None
        if clean is not None and not 2 <= len(clean) <= 128:
            raise ValueError("card_holder must contain 2..128 characters")
        return clean

    @field_validator("usdt_address", "ton_address")
    @classmethod
    def validate_address(cls, value: str | None) -> str | None:
        clean = value.strip() if value else None
        if clean is not None and not 3 <= len(clean) <= 128:
            raise ValueError("wallet address must contain 3..128 characters")
        return clean

    @model_validator(mode="after")
    def require_change(self) -> StorefrontPaymentSettingsBody:
        if not self.model_fields_set:
            raise ValueError("at least one payment field is required")
        for key in ("pay_card_enabled", "pay_usdt_enabled", "pay_ton_enabled"):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f"{key} cannot be null")
        return self


class StorefrontTrialSettingsOut(BaseModel):
    free_trial_enabled: bool
    free_trial_gb: int
    free_trial_days: int


class StorefrontTrialSettingsBody(StorefrontStrictBody):
    free_trial_enabled: bool | None = None
    free_trial_gb: int | None = Field(default=None, ge=1, le=1_000)
    free_trial_days: int | None = Field(default=None, ge=1, le=90)

    @model_validator(mode="after")
    def require_change(self) -> StorefrontTrialSettingsBody:
        if not self.model_fields_set:
            raise ValueError("at least one trial field is required")
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("trial fields cannot be null")
        return self


class StorefrontMessageSettingsOut(BaseModel):
    welcome_text: str | None
    support_contact: str | None


class StorefrontMessageSettingsBody(StorefrontStrictBody):
    welcome_text: str | None = Field(default=None, max_length=1_000)
    support_contact: str | None = Field(default=None, max_length=128)

    @field_validator("welcome_text", "support_contact")
    @classmethod
    def clean_optional(cls, value: str | None) -> str | None:
        clean = value.strip() if value else None
        return clean or None

    @model_validator(mode="after")
    def require_change(self) -> StorefrontMessageSettingsBody:
        if not self.model_fields_set:
            raise ValueError("at least one message field is required")
        return self


class StorefrontShopStateOut(BaseModel):
    shop_closed: bool
    closed_text: str | None


class StorefrontShopStateBody(StorefrontStrictBody):
    shop_closed: bool | None = None
    closed_text: str | None = Field(default=None, max_length=1_000)

    @field_validator("closed_text")
    @classmethod
    def clean_closed_text(cls, value: str | None) -> str | None:
        clean = value.strip() if value else None
        return clean or None

    @model_validator(mode="after")
    def require_change(self) -> StorefrontShopStateBody:
        if not self.model_fields_set:
            raise ValueError("at least one shop-state field is required")
        if "shop_closed" in self.model_fields_set and self.shop_closed is None:
            raise ValueError("shop_closed cannot be null")
        return self


class StorefrontChannelOut(BaseModel):
    channel_id: str | None
    channel_link: str | None
    channel_required: bool
    verified: bool
    health: Literal["ok", "disabled", "error"]
    verified_at: dt.datetime | None
    verification_error: str | None


class StorefrontChannelBody(StorefrontStrictBody):
    channel_id: str = Field(min_length=2, max_length=64)

    @field_validator("channel_id")
    @classmethod
    def validate_channel(cls, value: str) -> str:
        clean = value.strip()
        if not (re.fullmatch(r"@[A-Za-z0-9_]{5,32}", clean) or re.fullmatch(r"-?\d+", clean)):
            raise ValueError("channel_id must be @username or a numeric Telegram chat id")
        return clean


class StorefrontSettingsOut(BaseModel):
    payment: StorefrontPaymentSettingsOut
    trial: StorefrontTrialSettingsOut
    messages: StorefrontMessageSettingsOut
    shop_state: StorefrontShopStateOut
    channel: StorefrontChannelOut
    config_version: int = Field(ge=1)


class StorefrontManagerOut(BaseModel):
    telegram_id: str
    role: Literal["owner", "manager"]
    removable: bool


class StorefrontManagersOut(BaseModel):
    owner_id: str
    items: list[StorefrontManagerOut]
    max_count: int = 10
    config_version: int = Field(ge=1)


class StorefrontManagerBody(StorefrontStrictBody):
    telegram_id: int = Field(gt=0, le=9_223_372_036_854_775_807)


class StorefrontPreviewFreeTrialOut(BaseModel):
    enabled: bool
    gb: int
    days: int


class StorefrontPreviewPlanOut(BaseModel):
    id: int
    title: str
    gb: int
    days: int
    price_toman: int


class StorefrontCustomerPreviewOut(BaseModel):
    welcome_text: str
    support_contact: str | None
    shop_closed: bool
    closed_text: str | None
    free_trial: StorefrontPreviewFreeTrialOut
    payment_methods: list[Literal["card", "usdt", "ton"]]
    enabled_plans: list[StorefrontPreviewPlanOut]
    channel_required: bool


# ── customer & order management (plan 004) ───────────────────────────────────
class StorefrontCustomerStatusBody(StorefrontStrictBody):
    banned: bool
    reason: str = Field(min_length=3, max_length=255)


class StorefrontOrderEnabledBody(StorefrontStrictBody):
    enabled: bool


class StorefrontOrderDeleteBody(StorefrontStrictBody):
    confirm: Literal["DELETE"]
    reason: str = Field(min_length=3, max_length=255)
