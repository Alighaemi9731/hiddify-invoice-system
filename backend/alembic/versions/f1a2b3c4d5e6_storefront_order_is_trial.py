"""storefront_orders.is_trial — mark free-trial configs (non-renewable, excluded from billing)

Revision ID: f1a2b3c4d5e6
Revises: e4f7b1c9a2d5
Create Date: 2026-07-04

Adds a boolean `is_trial` (default false) and backfills existing trial orders — identified by the
runtime trial signature `plan_id IS NULL AND price_toman = 0` (a paid order whose plan was later
deleted keeps `price_toman > 0`, so the AND is safe). Free trials are never renewable and are
excluded from the reseller's invoice. `server_default=sa.false()` is required on PostgreSQL BOOLEAN
(text("0") fails there); the backfill uses the dialect-safe `sa.table(...).update()` form.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e4f7b1c9a2d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "storefront_orders",
        sa.Column("is_trial", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    orders = sa.table(
        "storefront_orders",
        sa.column("is_trial", sa.Boolean()),
        sa.column("plan_id", sa.Integer()),
        sa.column("price_toman", sa.Integer()),
    )
    op.execute(
        orders.update()
        .where(sa.and_(orders.c.plan_id.is_(None), orders.c.price_toman == 0))
        .values(is_trial=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("storefront_orders") as batch_op:
        batch_op.drop_column("is_trial")
