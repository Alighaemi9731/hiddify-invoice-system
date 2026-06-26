"""storefront Telegram ids -> BigInteger

Revision ID: f2b9c7a1d3e8
Revises: 557ce30f0d9c
Create Date: 2026-06-27

Telegram bot/user ids exceed int32, so storefront_bots.bot_telegram_id and
storefront_customers.telegram_id (originally Integer) overflowed on Postgres ("value out of int32
range"), breaking storefront setup + customer creation. Widen them to BIGINT. The storefront tables
are empty in production, so the type change is trivially safe.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2b9c7a1d3e8"
down_revision: str | None = "557ce30f0d9c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("storefront_bots") as batch_op:
        batch_op.alter_column(
            "bot_telegram_id", existing_type=sa.Integer(), type_=sa.BigInteger(),
            existing_nullable=True,
        )
    with op.batch_alter_table("storefront_customers") as batch_op:
        batch_op.alter_column(
            "telegram_id", existing_type=sa.Integer(), type_=sa.BigInteger(),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("storefront_customers") as batch_op:
        batch_op.alter_column(
            "telegram_id", existing_type=sa.BigInteger(), type_=sa.Integer(),
            existing_nullable=False,
        )
    with op.batch_alter_table("storefront_bots") as batch_op:
        batch_op.alter_column(
            "bot_telegram_id", existing_type=sa.BigInteger(), type_=sa.Integer(),
            existing_nullable=True,
        )
