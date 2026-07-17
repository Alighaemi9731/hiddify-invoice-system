"""storefront customer/order keyset indexes + order live-refresh throttle

Revision ID: 6e34a6ce638a
Revises: 1b84c0a7d3e5
Create Date: 2026-07-17
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "6e34a6ce638a"
down_revision: str | None = "1b84c0a7d3e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Stable keyset pagination for the web customer list and per-customer order list.
    op.create_index(
        "ix_sfcustomer_keyset", "storefront_customers",
        ["storefront_bot_id", "created_at", "id"],
    )
    op.create_index(
        "ix_sforder_customer_keyset", "storefront_orders",
        ["customer_id", "status", "created_at", "id"],
    )
    # Cross-process throttle timestamp for the per-order explicit live refresh.
    with op.batch_alter_table("storefront_orders", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("live_refreshed_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("storefront_orders", schema=None) as batch_op:
        batch_op.drop_column("live_refreshed_at")
    op.drop_index("ix_sforder_customer_keyset", table_name="storefront_orders")
    op.drop_index("ix_sfcustomer_keyset", table_name="storefront_customers")
