"""storefront subscriptions + retention + reliability

Revision ID: c7a2f4e9d1b6
Revises: b4e1d2f7a9c3
Create Date: 2026-06-27

Adds the columns/indexes for the v1.44.0 storefront rework:
  - storefront_orders.panel_id, .last_renewed_at (+ (panel_id, panel_user_uuid) index) — durable
    reference to the real panel user (denormalized, no hard FK).
  - storefront_wallet_txns.order_id (+ index) — link money to its order for the pending-order reaper.
  - storefront_customers.last_seen_at — drives retention.
  - free trial default-on: backfill existing storefront_bots to enabled.
  - partial-unique index on storefront_bots.bot_telegram_id (WHERE NOT NULL) — one bot ↔ one tenant.

All additive + nullable; portable to Postgres and SQLite. The boolean backfill is rendered per-dialect
by SQLAlchemy core (avoids the PG "DEFAULT 0 for boolean" trap).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7a2f4e9d1b6"
down_revision: str | None = "b4e1d2f7a9c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("storefront_orders", sa.Column("panel_id", sa.Integer(), nullable=True))
    op.add_column("storefront_orders",
                  sa.Column("last_renewed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("storefront_wallet_txns", sa.Column("order_id", sa.Integer(), nullable=True))
    op.add_column("storefront_customers",
                  sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))

    op.create_index("ix_storefront_order_panel_user", "storefront_orders",
                    ["panel_id", "panel_user_uuid"])
    op.create_index("ix_storefront_wallet_txns_order_id", "storefront_wallet_txns", ["order_id"])
    op.create_index("uq_storefront_bot_tgid", "storefront_bots", ["bot_telegram_id"], unique=True,
                    postgresql_where=sa.text("bot_telegram_id IS NOT NULL"),
                    sqlite_where=sa.text("bot_telegram_id IS NOT NULL"))

    # Free trial is now on by default for everyone — backfill existing storefronts.
    bots = sa.table("storefront_bots", sa.column("free_trial_enabled", sa.Boolean()))
    op.execute(bots.update().values(free_trial_enabled=True))


def downgrade() -> None:
    op.drop_index("uq_storefront_bot_tgid", table_name="storefront_bots")
    op.drop_index("ix_storefront_wallet_txns_order_id", table_name="storefront_wallet_txns")
    op.drop_index("ix_storefront_order_panel_user", table_name="storefront_orders")
    op.drop_column("storefront_customers", "last_seen_at")
    op.drop_column("storefront_wallet_txns", "order_id")
    op.drop_column("storefront_orders", "last_renewed_at")
    op.drop_column("storefront_orders", "panel_id")
