"""storefront wallet: tenant column + replay/refund uniqueness (security Batch 2)

Revision ID: f5b8d1a3c6e9
Revises: e3a5c7f9b1d4
Create Date: 2026-07-15

Security remediation Batch 2 (docs/SECURITY_REVIEW_PLAN.md):
- F14: adds `storefront_wallet_txns.storefront_bot_id` (backfilled from the customer) and a
  TENANT-SCOPED partial-unique index on the NORMALIZED crypto txid, so one on-chain deposit can be
  credited at most once per shop (replay protection the owner-payment path already had).
- F5: a partial-unique index on `(order_id) WHERE kind='refund'` — the durable backstop against a
  reaper/provisioner double refund. Partial, so repeat `purchase`/`renew_reversal` rows per order
  (legitimate renewals / F11 compensation) are unaffected.

Pre-existing data is normalized and de-duplicated BEFORE the unique indexes are built (else the
build would fail): crypto txids are lowercased; any residual same-shop txid collision keeps the
earliest row and NULLs the txid of the rest; any residual per-order refund collision keeps the
earliest and relabels the rest to `refund_dup` (audit-only — the balance is already what it is).
Partial indexes are portable via sqlite_where/postgresql_where (the contracts test runs on SQLite).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f5b8d1a3c6e9"
down_revision: str | None = "e3a5c7f9b1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CRYPTO = "method IN ('usdt','ton')"


def upgrade() -> None:
    op.add_column(
        "storefront_wallet_txns",
        sa.Column("storefront_bot_id", sa.Integer(), nullable=True),
    )
    # Backfill the tenant from the owning customer.
    op.execute(
        "UPDATE storefront_wallet_txns SET storefront_bot_id = ("
        "  SELECT c.storefront_bot_id FROM storefront_customers c"
        "  WHERE c.id = storefront_wallet_txns.customer_id) "
        "WHERE storefront_bot_id IS NULL"
    )
    # Normalize crypto txids so casing/whitespace variants of ONE transfer collide (matches the
    # app's create_topup: strip + lower). trim() is portable across SQLite and Postgres.
    op.execute(
        f"UPDATE storefront_wallet_txns SET txid = lower(trim(txid)) "
        f"WHERE txid IS NOT NULL AND {_CRYPTO}"
    )
    # De-dup residual same-shop crypto txids (keep earliest, NULL the rest) before the unique index.
    op.execute(
        f"UPDATE storefront_wallet_txns SET txid = NULL "
        f"WHERE {_CRYPTO} AND txid IS NOT NULL AND id NOT IN ("
        f"  SELECT MIN(id) FROM storefront_wallet_txns"
        f"  WHERE {_CRYPTO} AND txid IS NOT NULL GROUP BY storefront_bot_id, txid)"
    )
    # De-dup residual per-order refunds (keep earliest, relabel the rest) before the unique index.
    op.execute(
        "UPDATE storefront_wallet_txns SET kind = 'refund_dup' "
        "WHERE kind = 'refund' AND order_id IS NOT NULL AND id NOT IN ("
        "  SELECT MIN(id) FROM storefront_wallet_txns"
        "  WHERE kind = 'refund' AND order_id IS NOT NULL GROUP BY order_id)"
    )

    op.create_index(
        "ix_storefront_wallet_txns_storefront_bot_id", "storefront_wallet_txns",
        ["storefront_bot_id"],
    )
    op.create_index(
        "uq_sfwallet_txid_per_bot", "storefront_wallet_txns",
        ["storefront_bot_id", "txid"], unique=True,
        sqlite_where=sa.text(f"txid IS NOT NULL AND {_CRYPTO}"),
        postgresql_where=sa.text(f"txid IS NOT NULL AND {_CRYPTO}"),
    )
    op.create_index(
        "uq_sfwallet_refund_per_order", "storefront_wallet_txns",
        ["order_id"], unique=True,
        sqlite_where=sa.text("kind = 'refund'"),
        postgresql_where=sa.text("kind = 'refund'"),
    )


def downgrade() -> None:
    op.drop_index("uq_sfwallet_refund_per_order", table_name="storefront_wallet_txns")
    op.drop_index("uq_sfwallet_txid_per_bot", table_name="storefront_wallet_txns")
    op.drop_index("ix_storefront_wallet_txns_storefront_bot_id", table_name="storefront_wallet_txns")
    op.drop_column("storefront_wallet_txns", "storefront_bot_id")
