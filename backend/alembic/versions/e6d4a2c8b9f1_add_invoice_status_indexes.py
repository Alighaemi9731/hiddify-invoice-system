"""add invoice status indexes

Revision ID: e6d4a2c8b9f1
Revises: d5b8e3f2a017
Create Date: 2026-07-02

Dunning, the reseller portal, and enforcement constantly filter invoices on `status`
(`status IN (sent, overdue, enforced)`) — usually together with `reseller_id`. Neither
column combination was indexed, so those queries scanned the whole invoices table as it
grows. Additive DDL only; safe on both PostgreSQL and SQLite, instant at current sizes.
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e6d4a2c8b9f1"
down_revision: str | None = "d5b8e3f2a017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_invoices_status", "invoices", ["status"])
    op.create_index("ix_invoices_reseller_status", "invoices", ["reseller_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_invoices_reseller_status", table_name="invoices")
    op.drop_index("ix_invoices_status", table_name="invoices")
