"""storefront_orders.expiry_alerted_at for near-expiry reminders

Revision ID: a8c5d7e2f4b6
Revises: f7a3b5d9c2e4
Create Date: 2026-07-02

Additive nullable column: when the near-expiry reminder for an order was last sent
(the daily `storefront_expiry` job dedups on it; renewal re-arms it by comparing to
`last_renewed_at`). Safe on PostgreSQL and SQLite.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a8c5d7e2f4b6"
down_revision: str | None = "f7a3b5d9c2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "storefront_orders",
        sa.Column("expiry_alerted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("storefront_orders", "expiry_alerted_at")
