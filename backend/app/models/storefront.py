"""Multi-tenant VPN storefront bots.

Each top-level reseller can run their OWN Telegram storefront bot (their own BotFather token, one of
their registered panels) to sell VPN to THEIR customers. A customer tops up a wallet (reseller confirms
the deposit), then buys a plan; the bot auto-creates a config on the reseller's panel (as the reseller,
so it counts toward the reseller's own usage that the owner bills) and sends the sub link + QR.

All money here is the reseller↔customer ledger — entirely separate from the owner↔reseller invoices.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import TimestampMixin


class StorefrontBot(Base, TimestampMixin):
    """One storefront per top-level reseller. Holds the bot token (encrypted), the chosen panel,
    payment configuration, and the forced-join gate. `status`: 'active' | 'errored' (revoked token)."""
    __tablename__ = "storefront_bots"
    __table_args__ = (
        UniqueConstraint("reseller_id", name="uq_storefront_bot_reseller"),
        # One physical Telegram bot maps to exactly one tenant (when its id is known).
        Index("uq_storefront_bot_tgid", "bot_telegram_id", unique=True,
              sqlite_where=text("bot_telegram_id IS NOT NULL"),
              postgresql_where=text("bot_telegram_id IS NOT NULL")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    reseller_id: Mapped[int] = mapped_column(
        ForeignKey("resellers.id", ondelete="CASCADE"), index=True
    )
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="CASCADE"), index=True)

    bot_token_enc: Mapped[str] = mapped_column(String(512))          # Fernet-encrypted
    bot_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bot_telegram_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )  # the bot's own Telegram id (exceeds int32) → tenant resolution
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)

    # Storefront settings (the reseller edits these from their bot's admin side)
    support_contact: Mapped[str | None] = mapped_column(String(128), nullable=True)  # @handle / link
    welcome_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Payment configuration (reseller's OWN accounts)
    pay_card_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    pay_usdt_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    pay_ton_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    card_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    card_holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    usdt_address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ton_address: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Forced-join gate for customers (optional)
    channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel_link: Mapped[str | None] = mapped_column(String(255), nullable=True)
    channel_required: Mapped[bool] = mapped_column(Boolean, default=False)

    # One-time free trial (each customer can claim it once). Default ON for everyone, 1 GB · 1 day —
    # at/under the owner's free-config threshold, so the trial config is free for the reseller too.
    free_trial_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    free_trial_gb: Mapped[int] = mapped_column(Integer, default=1)
    free_trial_days: Mapped[int] = mapped_column(Integer, default=1)

    # Extra Telegram user-ids allowed to manage this shop (comma-separated), in addition to the
    # owning reseller's own `bot_chat_id`. Lets the shop owner appoint co-managers.
    co_admin_ids: Mapped[str | None] = mapped_column(Text, nullable=True)

    # «Temporarily closed» switch: when on, customers can't buy/renew (they see `closed_text`), but
    # the bot stays online and the rest of the menu (my services, wallet) still works. Distinct from
    # `enabled` (which stops the bot's polling entirely).
    shop_closed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    closed_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    plans: Mapped[list[StorefrontPlan]] = relationship(
        back_populates="bot", cascade="all, delete-orphan"
    )


class StorefrontPlan(Base, TimestampMixin):
    __tablename__ = "storefront_plans"
    __table_args__ = (
        CheckConstraint("gb >= 0", name="ck_storefront_plan_gb_nonneg"),
        CheckConstraint("days >= 0", name="ck_storefront_plan_days_nonneg"),
        CheckConstraint("price_toman >= 0", name="ck_storefront_plan_price_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    storefront_bot_id: Mapped[int] = mapped_column(
        ForeignKey("storefront_bots.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(128), default="")
    gb: Mapped[int] = mapped_column(Integer, default=0)
    days: Mapped[int] = mapped_column(Integer, default=0)
    price_toman: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    bot: Mapped[StorefrontBot] = relationship(back_populates="plans")


class StorefrontCustomer(Base, TimestampMixin):
    __tablename__ = "storefront_customers"
    __table_args__ = (
        UniqueConstraint("storefront_bot_id", "telegram_id", name="uq_storefront_customer"),
        CheckConstraint("wallet_balance_toman >= 0", name="ck_storefront_wallet_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    storefront_bot_id: Mapped[int] = mapped_column(
        ForeignKey("storefront_bots.id", ondelete="CASCADE"), index=True
    )
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)  # exceeds int32
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wallet_balance_toman: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    banned: Mapped[bool] = mapped_column(Boolean, default=False)
    free_trial_used: Mapped[bool] = mapped_column(Boolean, default=False)
    # Bumped on every interaction → drives retention (a tire-kicker who never returns is purged).
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StorefrontWalletTxn(Base, TimestampMixin):
    """Wallet ledger. `kind`: topup | purchase | manual_credit | manual_debit | refund.
    `amount_toman` is SIGNED (credit +, debit −). `status`: pending | confirmed | rejected | done.
    A topup stays `pending` until the reseller-admin confirms it; only then is the balance credited."""
    __tablename__ = "storefront_wallet_txns"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("storefront_customers.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    amount_toman: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    method: Mapped[str | None] = mapped_column(String(16), nullable=True)  # card|usdt|ton|manual
    # Links a purchase/refund to its order so the reaper can reconcile money ↔ provisioning.
    # Plain reference (no hard FK) so order lifecycle never cascades into the money ledger.
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    proof_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    txid: Mapped[str | None] = mapped_column(String(120), nullable=True)
    chain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StorefrontOrder(Base, TimestampMixin):
    """A purchase → one config on the reseller's panel. Plan figures are snapshotted so later plan
    edits don't rewrite history. `status`: pending | provisioned | disabled | failed | deleted.

    `(panel_id, panel_user_uuid)` is the durable reference to the real panel user — joinable to
    end_user_snapshots(panel_id, user_uuid) but NOT a hard FK (snapshots are pruned when a user/panel
    disappears, which must never cascade-delete order/ledger history)."""
    __tablename__ = "storefront_orders"
    __table_args__ = (
        Index("ix_storefront_order_panel_user", "panel_id", "panel_user_uuid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("storefront_customers.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("storefront_plans.id", ondelete="SET NULL"), nullable=True
    )
    # Plain reference (no hard FK) — the panel user is keyed by (panel_id, panel_user_uuid); a pruned
    # snapshot or deleted panel must never cascade into order history.
    panel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A free-trial config. Trials are NEVER renewable and are EXCLUDED from the reseller's invoice
    # (a free giveaway). Set on claim_trial; paid buys leave it False.
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)  # customer-chosen config name
    gb: Mapped[int] = mapped_column(Integer, default=0)
    days: Mapped[int] = mapped_column(Integer, default=0)
    price_toman: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    panel_user_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sub_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_renewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the near-expiry reminder was last sent (I10). Re-armed by renewal: a reminder is due
    # again once `last_renewed_at` is newer than this stamp. NULL = never notified.
    expiry_alerted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
