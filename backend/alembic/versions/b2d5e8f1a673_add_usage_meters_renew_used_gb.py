"""add usage_meters.renew_used_gb

Revision ID: b2d5e8f1a673
Revises: 9b1e4c72a5f8
Create Date: 2026-06-16

Stores the actual consumption of cycles legitimately renewed away within a month (capped at
each closed cycle's sold quota), so multiple in-month renewals aren't under-billed. NOT NULL
with a server default of 0 so existing rows are populated; the model declares the same
server_default, so there's no model/DB drift.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b2d5e8f1a673"
down_revision: str | None = "9b1e4c72a5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("usage_meters") as batch_op:
        batch_op.add_column(
            sa.Column("renew_used_gb", sa.Numeric(16, 3), nullable=False, server_default="0")
        )
        batch_op.create_check_constraint(
            "ck_usage_meters_renew_used_nonnegative", "renew_used_gb >= 0"
        )


def downgrade() -> None:
    with op.batch_alter_table("usage_meters") as batch_op:
        batch_op.drop_constraint("ck_usage_meters_renew_used_nonnegative", type_="check")
        batch_op.drop_column("renew_used_gb")
