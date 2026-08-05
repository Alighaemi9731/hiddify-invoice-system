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
    JSON,
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
    func,
    text,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.mixins import TimestampMixin


class StorefrontBot(Base, TimestampMixin):
    """One storefront per top-level reseller. Holds the bot token (encrypted), the chosen panel,
    payment configuration, and the forced-join gate.

    `status`:
      - 'active'  — polled by the manager; the only status that counts as live.
      - 'errored' — an AMBIGUOUS failure (e.g. two rows claiming one Telegram id). The token is
                    kept, because it may well still be valid.
      - 'revoked' — the credential is provably dead (401 / malformed). `bot_token_enc` is CLEARED
                    to "" and the owning reseller is notified once. The row and all its shop data
                    survive: re-running the setup wizard with a new token recovers it in place.
    """
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
    # Optimistic-concurrency token shared by the bot and web portal configuration commands.
    config_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )

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
    channel_verified_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    channel_verification_error: Mapped[str | None] = mapped_column(String(32), nullable=True)

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
        # Keyset pagination for the web customer list (per shop, newest first).
        Index("ix_sfcustomer_keyset", "storefront_bot_id", "created_at", "id"),
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
    """Wallet ledger. `kind`: topup | purchase | manual_credit | manual_debit | refund | renew_reversal
    | credit_bonus. `amount_toman` is SIGNED (credit +, debit −). `status`: pending | confirmed |
    rejected | done. A topup stays `pending` until the reseller-admin confirms it; only then credited."""
    __tablename__ = "storefront_wallet_txns"
    __table_args__ = (
        # F14: a crypto deposit is credited at most ONCE per shop — a tenant-scoped partial-unique
        # index on the normalized txid (only crypto rows carry a replay-protected txid).
        Index("uq_sfwallet_txid_per_bot", "storefront_bot_id", "txid", unique=True,
              sqlite_where=text("txid IS NOT NULL AND method IN ('usdt','ton')"),
              postgresql_where=text("txid IS NOT NULL AND method IN ('usdt','ton')")),
        # F5: at most ONE refund per order — the durable backstop against a reaper/provisioner double
        # refund (partial so repeat 'purchase'/'renew_reversal' rows per order are unaffected).
        Index("uq_sfwallet_refund_per_order", "order_id", unique=True,
              sqlite_where=text("kind = 'refund'"),
              postgresql_where=text("kind = 'refund'")),
        # F11 (round 2): at most ONE reversal per renewal OPERATION — the durable backstop so a crashed
        # renewal is compensated exactly once whether the inline handler or the reconciler does it
        # (partial WHERE kind='renew_reversal', so it never collides with the per-order refund index).
        Index("uq_sfwallet_reversal_per_operation", "operation_id", unique=True,
              sqlite_where=text("kind = 'renew_reversal'"),
              postgresql_where=text("kind = 'renew_reversal'")),
        # Auto-renew: at most ONE ACTIVE hold per order at a time (a partial unique on the still-held
        # rows). Disarm/settle flips a hold's status off `held`, so re-arming next cycle is allowed —
        # only two concurrent live holds on one order are refused. Arm/disarm/fire all take the order
        # row lock too, so this is a durable backstop, not the primary guard.
        Index("uq_sfwallet_active_hold_per_order", "order_id", unique=True,
              sqlite_where=text("kind = 'hold' AND status = 'held'"),
              postgresql_where=text("kind = 'hold' AND status = 'held'")),
        # Plan 005: the top-up operations queue/history (per shop, by kind+status, newest first).
        Index("ix_sfwallet_shop_kind_status_created",
              "storefront_bot_id", "kind", "status", "created_at", "id"),
        # Plan 005: a customer's ledger keyset (newest first).
        Index("ix_sfwallet_customer_created", "customer_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("storefront_customers.id", ondelete="CASCADE"), index=True
    )
    # Denormalized tenant (= customer.storefront_bot_id) so txid uniqueness can be scoped PER SHOP
    # by a partial unique index (a cross-table join can't back a unique constraint). NON-NULL for
    # every ledger kind (plan 005): all writers populate it from the locked customer, and the
    # migration backfilled + enforced it.
    storefront_bot_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    amount_toman: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    # The customer's ORIGINALLY requested top-up amount — immutable; only `create_topup` sets it.
    # `amount_toman` is overwritten with the admin-credited value on confirm (esp. crypto), so this
    # preserves requested-vs-credited for the audit. NULL for non-topup rows and legacy decided topups.
    requested_amount_toman: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    method: Mapped[str | None] = mapped_column(String(16), nullable=True)  # card|usdt|ton|manual
    # Links a purchase/refund to its order so the reaper can reconcile money ↔ provisioning.
    # Plain reference (no hard FK) so order lifecycle never cascades into the money ledger.
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # F4/F11 (round 2): links a debit/reversal to its durable StorefrontOperation so a renewal is
    # charged/reversed exactly once. Plain reference (no hard FK).
    operation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    proof_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    txid: Mapped[str | None] = mapped_column(String(120), nullable=True)
    chain: Mapped[str | None] = mapped_column(String(16), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # A pending top-up may carry a credit code (کد شارژ) captured at request time; the bonus is
    # applied and the code consumed only when the admin CONFIRMS the top-up. Plain reference.
    credit_code_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class StorefrontOrder(Base, TimestampMixin):
    """A purchase → one config on the reseller's panel. Plan figures are snapshotted so later plan
    edits don't rewrite history. `status`: pending | provisioned | disabled | failed | deleted, plus
    the transient `renewing` (held during a renew) — read models treat `renewing` as an active,
    in-flight service, never as gone.

    `(panel_id, panel_user_uuid)` is the durable reference to the real panel user — joinable to
    end_user_snapshots(panel_id, user_uuid) but NOT a hard FK (snapshots are pruned when a user/panel
    disappears, which must never cascade-delete order/ledger history)."""
    __tablename__ = "storefront_orders"
    __table_args__ = (
        Index("ix_storefront_order_panel_user", "panel_id", "panel_user_uuid"),
        # Keyset pagination for a customer's order list (newest first, status-filterable).
        Index("ix_sforder_customer_keyset", "customer_id", "status", "created_at", "id"),
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
    # Cross-process throttle stamp for the owner's per-order explicit live refresh (plan 004). Set
    # under a row lock before the panel read; a repeat within `storefront_live_refresh_seconds` → 429.
    live_refreshed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # F5/F11 (round 2): a durable provisioning/renewal LEASE. While `lease_expires_at` is in the future
    # the reaper/reconciler leaves this order alone (a live purchase/renew is holding it across the panel
    # network call). Set before the network I/O, cleared on finalize. NULL = no active lease.
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When the near-expiry reminder was last sent (I10). Re-armed by renewal: a reminder is due
    # again once `last_renewed_at` is newer than this stamp. NULL = never notified.
    expiry_alerted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # When the «free trial ended → buy a plan» nudge was sent (once per trial). NULL = never.
    trial_ended_alerted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # When the «~80% of your volume used» warning was sent. Re-armed by a renewal (fresh quota) the
    # same way `expiry_alerted_at` is. NULL = never.
    # Win-back after a PAID service actually lapsed. Re-armed by a renewal, like the others.
    expired_alerted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    usage_alerted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    # Deferred ONE-SHOT auto-renew: the customer armed it, so the plan price is LOCKED (a wallet
    # `hold` txn) and the near-exhaustion fire job renews ONCE when the config is nearly out of
    # volume/days, then clears these (one-shot — must be re-armed each cycle). `armed_at` set on arm,
    # cleared on disarm/fire. `price_toman` is the price locked AT ARM TIME (so a later plan-price
    # change doesn't affect it). `hold_txn_id` links the reserved funds so a disarm/failed-fire can
    # release exactly that hold. NULL across all three = not armed.
    autorenew_armed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    autorenew_price_toman: Mapped[int | None] = mapped_column(Integer, nullable=True)
    autorenew_hold_txn_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class StorefrontOperation(Base, TimestampMixin):
    """A durable, idempotent record of one money-moving storefront action (a purchase or a renewal),
    keyed by a random `op_id` minted BEFORE the confirm button is shown (F4). It makes replays
    exactly-once — a repeat tap / re-delivered callback / cross-process retry with the same `op_id`
    returns the cached terminal result (`result_order_id`/`result_sub_link`) with no second debit or
    panel call. It also anchors crash recovery (F11): the debit is linked here, so the reconciler can
    reverse a renewal charge exactly once if the process died mid-renew. Denormalized, no hard FKs."""
    __tablename__ = "storefront_operations"
    __table_args__ = (
        Index("uq_storefront_operation_op_id", "op_id", unique=True),
        Index("ix_storefront_operation_status_order", "status", "order_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    op_id: Mapped[str] = mapped_column(String(64))                 # random token; unique
    op_type: Mapped[str] = mapped_column(String(16))              # purchase | renewal
    storefront_bot_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    customer_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    order_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    plan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # pending → in_progress → done | failed | reversed
    status: Mapped[str] = mapped_column(String(16), default="pending")
    prior_status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # renewal: restore target
    price_toman: Mapped[int] = mapped_column(Integer, default=0)
    gb: Mapped[int] = mapped_column(Integer, default=0)
    days: Mapped[int] = mapped_column(Integer, default=0)
    by_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    debit_txn_id: Mapped[int | None] = mapped_column(Integer, nullable=True)      # the wallet debit
    # Renewal recovery target, persisted BEFORE the debit/panel PATCH. If the worker dies after the
    # remote write, the reconciler verifies or idempotently reapplies this absolute target instead of
    # blindly refunding a renewal that the customer already received.
    target_usage_limit_gb: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    prior_panel_start_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # cached terminal result
    result_sub_link: Mapped[str | None] = mapped_column(Text, nullable=True)


class StorefrontAuditEvent(Base):
    """Append-only, redacted audit history for storefront administration."""
    __tablename__ = "storefront_audit_events"
    __table_args__ = (
        CheckConstraint("source in ('bot','portal','system')", name="ck_sfaudit_source"),
        Index("ix_sfaudit_shop_created", "storefront_bot_id", "created_at"),
        Index("ix_sfaudit_actor_action", "actor_telegram_id", "action"),
        Index("ix_sfaudit_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    storefront_bot_id: Mapped[int | None] = mapped_column(
        ForeignKey("storefront_bots.id", ondelete="SET NULL"), nullable=True
    )
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    actor_role: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    before_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    outcome: Mapped[str] = mapped_column(String(16))
    error_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class StorefrontApiCommand(Base, TimestampMixin):
    """Leased idempotency record shared by storefront bot and portal commands."""
    __tablename__ = "storefront_api_commands"
    __table_args__ = (
        UniqueConstraint(
            "storefront_bot_id", "actor_telegram_id", "idempotency_key",
            name="uq_sfcommand_actor_key",
        ),
        CheckConstraint(
            "status in ('pending','succeeded','failed','unknown')",
            name="ck_sfcommand_status",
        ),
        Index("ix_sfcommand_status_lease", "status", "lease_expires_at"),
        Index("ix_sfcommand_shop_updated", "storefront_bot_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    storefront_bot_id: Mapped[int] = mapped_column(
        ForeignKey("storefront_bots.id", ondelete="CASCADE"), nullable=False
    )
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    external_io: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    error_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )


class StorefrontCreditCode(Base, TimestampMixin):
    """A wallet TOP-UP credit code («کد شارژ/هدیه») scoped to one shop. Redeeming it credits the
    customer's wallet — a percentage bonus on a top-up (capped by `max_bonus_toman`), a fixed bonus
    on a top-up, or (when `is_gift`) a standalone fixed gift credited with NO payment. Never touches
    plan prices — it's purely the reseller↔customer wallet ledger."""
    __tablename__ = "storefront_credit_codes"
    __table_args__ = (
        # Case-insensitive uniqueness per shop (match + write both go through the normalized form).
        UniqueConstraint("storefront_bot_id", "code_ci", name="uq_storefront_credit_code"),
        CheckConstraint("kind in ('percent','fixed')", name="ck_storefront_credit_kind"),
        CheckConstraint("percent_off is null or (percent_off > 0 and percent_off <= 100)",
                        name="ck_storefront_credit_percent"),
        CheckConstraint("amount_toman is null or amount_toman > 0", name="ck_storefront_credit_amount"),
        CheckConstraint("used_count >= 0", name="ck_storefront_credit_used_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    storefront_bot_id: Mapped[int] = mapped_column(
        ForeignKey("storefront_bots.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(32))            # display form (as the admin typed it)
    code_ci: Mapped[str] = mapped_column(String(32))         # normalized (upper) for match + uniqueness
    kind: Mapped[str] = mapped_column(String(16))            # percent | fixed
    percent_off: Mapped[int | None] = mapped_column(Integer, nullable=True)       # 1..100
    amount_toman: Mapped[int | None] = mapped_column(Integer, nullable=True)      # fixed bonus (Toman)
    max_bonus_toman: Mapped[int | None] = mapped_column(Integer, nullable=True)   # cap for percent
    min_topup_toman: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    is_gift: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)          # total; NULL=unlimited
    per_customer_limit: Mapped[int | None] = mapped_column(
        Integer, default=1, server_default=text("1"))                            # NULL=unlimited
    used_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    starts_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # Archive (plan 006): archiving atomically sets enabled=False AND stamps this. The row + code_ci
    # unique key REMAIN so a normalized code can never be reused, and redemption history is preserved
    # (archive replaces the old hard delete). NULL = active.
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StorefrontCreditRedemption(Base, TimestampMixin):
    """One redemption of a credit code by a customer — drives per-customer + global usage counting
    and the admin audit/analytics view."""
    __tablename__ = "storefront_credit_redemptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    code_id: Mapped[int] = mapped_column(
        ForeignKey("storefront_credit_codes.id", ondelete="CASCADE"), index=True
    )
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("storefront_customers.id", ondelete="CASCADE"), index=True
    )
    # Plain reference (no hard FK) — mirrors StorefrontWalletTxn.order_id so the ledger never cascades.
    wallet_txn_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    bonus_toman: Mapped[int] = mapped_column(Integer, default=0)


class StorefrontBroadcastJob(Base, TimestampMixin):
    """A durable delivery job (plan 006): a one-shot broadcast to a customer segment, OR a single
    `direct` message (same model, kind='direct'). Recipients are snapshotted at create time into
    StorefrontDeliveryRecipient and delivered by the scheduler's delivery worker. Aggregate counts +
    audit are kept permanently; recipient detail rows are pruned after a retention window."""
    __tablename__ = "storefront_broadcast_jobs"
    __table_args__ = (
        CheckConstraint("kind in ('broadcast','direct')", name="ck_sfbjob_kind"),
        CheckConstraint("status in ('queued','running','completed','canceled')",
                        name="ck_sfbjob_status"),
        Index("ix_sfbjob_shop_created", "storefront_bot_id", "created_at"),
        Index("ix_sfbjob_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    storefront_bot_id: Mapped[int] = mapped_column(
        ForeignKey("storefront_bots.id", ondelete="CASCADE"), index=True)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))                # broadcast | direct
    segment: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'queued'"))
    total_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    sent_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    blocked_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    failed_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    pending_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    canceled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StorefrontDeliveryRecipient(Base, TimestampMixin):
    """One recipient of a StorefrontBroadcastJob. The delivery worker claims `pending`/`retry_wait`
    rows whose `next_attempt_at` is due with FOR UPDATE SKIP LOCKED, leases them (`sending` +
    `lease_expires_at`), sends OUTSIDE any DB transaction via the shop's own bot credential, then
    records a terminal (`sent|blocked|failed|canceled`) or a `retry_wait`. An expired `sending`
    lease becomes `unknown` and is retried once — delivery is at-least-once."""
    __tablename__ = "storefront_delivery_recipients"
    __table_args__ = (
        CheckConstraint(
            "status in ('pending','sending','retry_wait','sent','blocked','failed','unknown','canceled')",
            name="ck_sfdr_status"),
        UniqueConstraint("job_id", "customer_id", name="uq_sfdr_job_customer"),
        Index("ix_sfdr_claimable", "status", "next_attempt_at", "lease_expires_at"),
        Index("ix_sfdr_job_status", "job_id", "status"),
        Index("ix_sfdr_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("storefront_broadcast_jobs.id", ondelete="CASCADE"), index=True)
    customer_id: Mapped[int] = mapped_column(
        ForeignKey("storefront_customers.id", ondelete="CASCADE"), index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), server_default=text("'pending'"))
    attempt_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_class: Mapped[str | None] = mapped_column(String(64), nullable=True)
