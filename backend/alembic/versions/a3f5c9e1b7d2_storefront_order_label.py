"""storefront_orders.label — customer-chosen config name

Revision ID: a3f5c9e1b7d2
Revises: f2b9c7a1d3e8
Create Date: 2026-06-27

Each storefront purchase now lets the customer name their config; the name is the Hiddify user name +
the sub-link slug, and is shown in «my services». Persist it on the order so the list can label each
service distinctly. Nullable string add — portable to Postgres and SQLite; existing rows stay NULL.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a3f5c9e1b7d2"
down_revision: str | None = "f2b9c7a1d3e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("storefront_orders", sa.Column("label", sa.String(64), nullable=True))


def downgrade() -> None:
    op.drop_column("storefront_orders", "label")
