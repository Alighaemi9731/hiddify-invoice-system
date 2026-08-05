"""Owner-side, FLEET-WIDE storefront analytics (every reseller's shop bot at once).

Distinct from `portal_storefront`, which reports on ONE storefront for its own reseller. Everything
here is aggregated across all shops and is read-only — it never exposes a shop's secrets (token,
card number, wallet addresses) or an individual customer's identity.
"""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel


class SalesWindowOut(BaseModel):
    """Money moved in one time window, from the reseller↔customer wallet ledger.

    `gross` = what customers were charged, `reversals` = refunds/renewal reversals, `net` = the
    difference. The purchase/renewal split comes from the linked operation; ledger rows with no
    resolvable operation land in `unknown` so the buckets always add up to `gross`.
    """
    gross_toman: int
    reversals_toman: int
    net_toman: int
    orders: int
    purchase_count: int
    purchase_toman: int
    renewal_count: int
    renewal_toman: int
    unknown_count: int
    unknown_toman: int


class BotsOut(BaseModel):
    total: int
    enabled: int
    disabled: int
    active: int          # status == 'active'
    errored: int         # status == 'errored' — ambiguous failure, token kept
    revoked: int         # status == 'revoked' — token provably dead + cleared; needs a new one
    closed: int          # «فروشگاه موقتاً بسته است»
    selling: int         # enabled AND active AND not closed → actually able to sell right now
    without_plans: int
    trial_enabled: int
    channel_locked: int
    panel_unhealthy: int
    new_in_period: int
    eligible_resellers: int   # top-level resellers allowed a shop (the addressable market)


class CustomersOut(BaseModel):
    total: int
    new_today: int
    new_in_period: int
    active_7d: int
    active_30d: int
    banned: int
    buyers_in_period: int
    repeat_buyers_in_period: int
    wallet_liability_toman: int
    avg_order_toman: int
    arppu_toman: int          # net sales in period ÷ paying customers in period


class ServicesOut(BaseModel):
    total: int
    pending: int
    provisioned: int
    renewing: int
    disabled: int
    failed: int
    deleted: int
    active: int               # provisioned + renewing
    trials_active: int
    trials_in_period: int
    expiring_3d: int
    expiring_7d: int
    expired: int
    high_usage: int           # ≥80٪ of the sold quota already consumed
    quota_gb: float
    used_gb: float
    autorenew_armed: int


class MethodRowOut(BaseModel):
    method: str
    count: int
    amount_toman: int


class TopupsOut(BaseModel):
    pending_count: int
    pending_toman: int
    confirmed_count: int
    confirmed_toman: int
    rejected_count: int
    by_method: list[MethodRowOut]


class CreditsOut(BaseModel):
    redemptions: int
    bonus_toman: int
    active_codes: int


class OperationsOut(BaseModel):
    pending: int
    in_progress: int
    done: int
    failed: int
    reversed: int
    failed_24h: int


class TrialConversionOut(BaseModel):
    trial_customers: int
    converted_customers: int
    rate: float | None


class DailyPointOut(BaseModel):
    date: dt.date
    day: int
    net_toman: int
    orders: int
    new_customers: int
    topups_toman: int


class PlanShapeOut(BaseModel):
    """A plan SHAPE (GB × days) aggregated across every shop — plan rows are per-shop, so the
    fleet-wide best-seller question is only answerable at the shape level."""
    gb: int
    days: int
    orders: int
    amount_toman: int


class ShopRowOut(BaseModel):
    shop_id: int
    reseller_id: int
    reseller_name: str
    panel_key: str
    bot_username: str | None
    enabled: bool
    status: str
    shop_closed: bool
    health_error_class: str | None
    plans: int
    customers: int
    new_customers: int
    active_customers_30d: int
    services_active: int
    expiring_3d: int
    net_sales_toman: int
    orders: int
    today_net_toman: int
    wallet_liability_toman: int
    pending_topups_count: int
    pending_topups_toman: int
    last_sale_at: dt.datetime | None
    created_at: dt.datetime | None


class RevokedShopOut(BaseModel):
    """A shop whose bot token is dead. Kept out of the operational shops table (there is nothing
    to operate) and surfaced on its own so the owner can chase the reseller for a new token."""
    shop_id: int
    reseller_id: int
    reseller_name: str
    panel_key: str
    bot_username: str | None
    customers: int
    revoked_at: dt.datetime | None   # when the row was last written (i.e. when it went revoked)


class StorefrontAnalyticsOut(BaseModel):
    period: str
    period_start: dt.date
    period_end: dt.date
    previous_period: str
    generated_at: dt.datetime
    bots: BotsOut
    customers: CustomersOut
    services: ServicesOut
    topups: TopupsOut
    credits: CreditsOut
    operations: OperationsOut
    trial: TrialConversionOut
    sales_today: SalesWindowOut
    sales_yesterday: SalesWindowOut
    sales_7d: SalesWindowOut
    sales_30d: SalesWindowOut
    sales_period: SalesWindowOut
    sales_previous_period: SalesWindowOut
    daily: list[DailyPointOut]
    top_plans: list[PlanShapeOut]
    shops: list[ShopRowOut]
    revoked_shops: list[RevokedShopOut]
