"""storefront credit codes (کد شارژ/هدیه) — wallet top-up bonus + gift codes

Revision ID: e3a5c7f9b1d4
Revises: d1f3b5a7c9e2
Create Date: 2026-07-12

Adds `storefront_credit_codes` (per-shop codes: percent or fixed wallet bonus, optional standalone
gift, with active window / caps / per-customer + total usage limits) and
`storefront_credit_redemptions` (usage counting + audit), plus `storefront_wallet_txns.credit_code_id`
so a pending top-up can carry the code until the admin confirms it. Booleans use
`server_default=sa.false()/sa.true()` (Postgres BOOLEAN). No backfill (existing shops start with none).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e3a5c7f9b1d4"
down_revision: str | None = "d1f3b5a7c9e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "storefront_credit_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("storefront_bot_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("code_ci", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("percent_off", sa.Integer(), nullable=True),
        sa.Column("amount_toman", sa.Integer(), nullable=True),
        sa.Column("max_bonus_toman", sa.Integer(), nullable=True),
        sa.Column("min_topup_toman", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("is_gift", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("per_customer_limit", sa.Integer(), nullable=True, server_default=sa.text("1")),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["storefront_bot_id"], ["storefront_bots.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("storefront_bot_id", "code_ci", name="uq_storefront_credit_code"),
        sa.CheckConstraint("kind in ('percent','fixed')", name="ck_storefront_credit_kind"),
        sa.CheckConstraint("percent_off is null or (percent_off > 0 and percent_off <= 100)",
                           name="ck_storefront_credit_percent"),
        sa.CheckConstraint("amount_toman is null or amount_toman > 0", name="ck_storefront_credit_amount"),
        sa.CheckConstraint("used_count >= 0", name="ck_storefront_credit_used_nonneg"),
    )
    op.create_index("ix_storefront_credit_codes_storefront_bot_id", "storefront_credit_codes",
                    ["storefront_bot_id"])

    op.create_table(
        "storefront_credit_redemptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("wallet_txn_id", sa.Integer(), nullable=True),
        sa.Column("bonus_toman", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["code_id"], ["storefront_credit_codes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["storefront_customers.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_storefront_credit_redemptions_code_id", "storefront_credit_redemptions",
                    ["code_id"])
    op.create_index("ix_storefront_credit_redemptions_customer_id", "storefront_credit_redemptions",
                    ["customer_id"])
    op.create_index("ix_storefront_credit_redemptions_wallet_txn_id", "storefront_credit_redemptions",
                    ["wallet_txn_id"])

    op.add_column("storefront_wallet_txns",
                  sa.Column("credit_code_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("storefront_wallet_txns") as batch_op:
        batch_op.drop_column("credit_code_id")
    op.drop_table("storefront_credit_redemptions")
    op.drop_table("storefront_credit_codes")
