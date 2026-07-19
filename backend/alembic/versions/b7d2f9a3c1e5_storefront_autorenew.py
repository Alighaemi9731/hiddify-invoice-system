"""storefront_autorenew — deferred one-shot auto-renew

Adds the armed-state columns on a storefront order and the wallet backstop for the reserved funds:

  * `storefront_orders.autorenew_armed_at` / `autorenew_price_toman` / `autorenew_hold_txn_id` —
    NULL across all three means "not armed". When the customer arms auto-renew we lock the plan
    price (a wallet `hold` txn) and stamp these; the near-exhaustion fire job renews once and clears
    them (one-shot — re-armed each cycle).

  * `uq_sfwallet_active_hold_per_order` — a partial-unique index so at most ONE *live* hold
    (`kind='hold' AND status='held'`) can exist per order. Disarm/settle flips the hold's status off
    `held`, so re-arming next cycle is allowed; only two concurrent live holds are refused. Mirrors
    `uq_sfwallet_refund_per_order` / `uq_sfwallet_reversal_per_operation`. Portable via
    sqlite_where/postgresql_where (the contracts test runs on SQLite).

Fully additive and backward compatible: existing rows get NULL armed columns (not armed) and no
existing wallet row has kind='hold', so the index starts empty and locks nobody out.

Revision ID: b7d2f9a3c1e5
Revises: e8b3d5c7a2f1
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b7d2f9a3c1e5"
down_revision = "e8b3d5c7a2f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_orders",
        sa.Column("autorenew_armed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "storefront_orders",
        sa.Column("autorenew_price_toman", sa.Integer(), nullable=True),
    )
    op.add_column(
        "storefront_orders",
        sa.Column("autorenew_hold_txn_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        "uq_sfwallet_active_hold_per_order", "storefront_wallet_txns",
        ["order_id"], unique=True,
        sqlite_where=sa.text("kind = 'hold' AND status = 'held'"),
        postgresql_where=sa.text("kind = 'hold' AND status = 'held'"),
    )


def downgrade() -> None:
    op.drop_index("uq_sfwallet_active_hold_per_order", table_name="storefront_wallet_txns")
    op.drop_column("storefront_orders", "autorenew_hold_txn_id")
    op.drop_column("storefront_orders", "autorenew_price_toman")
    op.drop_column("storefront_orders", "autorenew_armed_at")
