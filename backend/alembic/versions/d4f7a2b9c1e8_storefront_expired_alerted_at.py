"""storefront_orders.expired_alerted_at — win-back notice after a paid service lapses

Revision ID: d4f7a2b9c1e8
Revises: 497cb88cf774
Create Date: 2026-07-18

Until now a customer heard nothing once their PAID service actually expired — the near-expiry
reminder deliberately stops at days_left < 0 — which is precisely the moment they are most likely
to renew. This stamp makes the new win-back notice fire once per service period (re-armed by a
renewal), exactly like `expiry_alerted_at`.
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d4f7a2b9c1e8"
down_revision = "497cb88cf774"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "storefront_orders",
        sa.Column("expired_alerted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("storefront_orders", "expired_alerted_at")
