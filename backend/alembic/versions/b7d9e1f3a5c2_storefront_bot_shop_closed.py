"""storefront_bots.shop_closed + closed_text — «temporarily closed» shop switch

Revision ID: b7d9e1f3a5c2
Revises: c2d4f6b8a1e3
Create Date: 2026-07-09

Adds a boolean `shop_closed` (default false) and a nullable TEXT `closed_text`. When a shop owner
flips the switch, customers can't buy/renew (they see `closed_text`) but the bot stays online. No
backfill — existing shops start open. `server_default=sa.false()` is required on PostgreSQL BOOLEAN.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7d9e1f3a5c2"
down_revision: str | None = "c2d4f6b8a1e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "storefront_bots",
        sa.Column("shop_closed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "storefront_bots",
        sa.Column("closed_text", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("storefront_bots") as batch_op:
        batch_op.drop_column("closed_text")
        batch_op.drop_column("shop_closed")
