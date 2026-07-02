"""add payment_settlements join table (mirror of Payment.settled_invoice_ids)

Revision ID: f7a3b5d9c2e4
Revises: e6d4a2c8b9f1
Create Date: 2026-07-02

The set of invoices a payment covers was stored ONLY as a comma-joined string
(`payments.settled_invoice_ids`), which is not queryable — the duplicate-pending block and
the revert-unpay protection loaded EVERY payment into Python and parsed strings on each
submission. This creates the indexed `payment_settlements(payment_id, invoice_id)` mirror
and backfills it from the comma column (falling back to the primary `invoice_id` link).

Dual-write from this release on: writers keep the comma column byte-equal, so rolling back
to the previous release is safe (old code reads the comma column and ignores this table).
Dangling invoice ids in legacy strings (invoice deleted since) are skipped and logged.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f7a3b5d9c2e4"
down_revision: str | None = "e6d4a2c8b9f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    table = op.create_table(
        "payment_settlements",
        sa.Column(
            "payment_id", sa.Integer(),
            sa.ForeignKey("payments.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column(
            "invoice_id", sa.Integer(),
            sa.ForeignKey("invoices.id", ondelete="CASCADE"), primary_key=True,
        ),
    )
    op.create_index("ix_payment_settlements_invoice_id", "payment_settlements", ["invoice_id"])

    conn = op.get_bind()
    valid_invoices = {
        r[0] for r in conn.execute(sa.text("SELECT id FROM invoices")).fetchall()
    }
    payments = conn.execute(
        sa.text("SELECT id, invoice_id, settled_invoice_ids FROM payments")
    ).fetchall()

    rows: list[dict[str, int]] = []
    seen: set[tuple[int, int]] = set()
    skipped = 0
    for pid, primary_id, raw in payments:
        ids: list[int] = []
        if raw:
            ids = [int(x) for x in str(raw).split(",") if x.strip().isdigit()]
        elif primary_id is not None:
            ids = [int(primary_id)]
        for iid in ids:
            if iid not in valid_invoices:
                skipped += 1
                continue
            key = (int(pid), iid)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"payment_id": int(pid), "invoice_id": iid})
    if rows:
        op.bulk_insert(table, rows)
    log.info(
        "payment_settlements backfill: %d rows from %d payments (%d dangling invoice ids skipped)",
        len(rows), len(payments), skipped,
    )


def downgrade() -> None:
    op.drop_index("ix_payment_settlements_invoice_id", table_name="payment_settlements")
    op.drop_table("payment_settlements")
