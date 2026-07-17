"""storefront wallet: non-null tenant id, requested top-up amount, ops-queue indexes

Revision ID: 525855401b89
Revises: 6e34a6ce638a
Create Date: 2026-07-17

Money-path migration (plan 005). The 4 previously-NULL wallet writers are patched to set
storefront_bot_id from the locked customer BEFORE this runs, so no new NULL rows appear. This
re-backfills any historical NULLs from the owning customer, HALTS if any row is unresolvable
(STOP condition), then enforces NOT NULL. Adds an immutable `requested_amount_toman` (backfilled
for still-pending top-ups; legacy decided rows stay NULL — the original request was overwritten on
confirm) and two composite indexes for the ops queue + customer ledger. No destructive cleanup.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "525855401b89"
down_revision: str | None = "6e34a6ce638a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "storefront_wallet_txns",
        sa.Column("requested_amount_toman", sa.Numeric(18, 2), nullable=True),
    )
    conn = op.get_bind()
    # Re-backfill the denormalized tenant id from the owning customer (idempotent).
    conn.execute(sa.text(
        "UPDATE storefront_wallet_txns SET storefront_bot_id = "
        "(SELECT c.storefront_bot_id FROM storefront_customers c "
        " WHERE c.id = storefront_wallet_txns.customer_id) "
        "WHERE storefront_bot_id IS NULL"
    ))
    # STOP guard: every ledger row must resolve to a shop. customer_id is a CASCADE FK so this cannot
    # normally fire; if it ever does, HALT and escalate rather than weaken the constraint.
    orphans = conn.execute(sa.text(
        "SELECT COUNT(*) FROM storefront_wallet_txns WHERE storefront_bot_id IS NULL"
    )).scalar()
    if orphans:
        raise RuntimeError(
            f"{orphans} wallet rows have an unresolvable storefront_bot_id — HALT "
            "(plan-005 STOP condition: orphaned/ambiguous wallet rows)"
        )
    # Enforce NOT NULL (PostgreSQL: native ALTER; SQLite: batch table rebuild).
    with op.batch_alter_table("storefront_wallet_txns") as batch:
        batch.alter_column("storefront_bot_id", existing_type=sa.Integer(), nullable=False)
    # Preserve the customer's originally-requested amount for still-pending top-ups.
    conn.execute(sa.text(
        "UPDATE storefront_wallet_txns SET requested_amount_toman = amount_toman "
        "WHERE kind = 'topup' AND status = 'pending' AND requested_amount_toman IS NULL"
    ))
    op.create_index(
        "ix_sfwallet_shop_kind_status_created", "storefront_wallet_txns",
        ["storefront_bot_id", "kind", "status", "created_at", "id"],
    )
    op.create_index(
        "ix_sfwallet_customer_created", "storefront_wallet_txns",
        ["customer_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_sfwallet_customer_created", table_name="storefront_wallet_txns")
    op.drop_index("ix_sfwallet_shop_kind_status_created", table_name="storefront_wallet_txns")
    with op.batch_alter_table("storefront_wallet_txns") as batch:
        batch.alter_column("storefront_bot_id", existing_type=sa.Integer(), nullable=True)
    op.drop_column("storefront_wallet_txns", "requested_amount_toman")
