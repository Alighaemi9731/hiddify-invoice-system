"""performance composite indexes (2026-07-22 audit, batch 2)

Revision ID: d7f3a9c1e5b8
Revises: c4d8e2f6a1b9
Create Date: 2026-07-22

The audit's hottest query families had no matching indexes:

- `end_user_snapshots WHERE panel_id=? AND added_by_uuid IN (...)` — every report,
  portal view, capacity meter, and the invoice engine's subtree reads. Only separate
  single-column indexes existed (bitmap-AND at best).
- The enforcement/reseller-count variant filters `lower(added_by_uuid)`, which a plain
  b-tree cannot serve at all → sequential scans on a tens-of-thousands-row table. UUIDs
  have been stored lowercase since `b1c3e5a7f9d2`, but the queries keep `lower()` as a
  defensive normalization, so the expression index matches them exactly.
- `usage_meters WHERE panel_id=? AND period_label=? [AND added_by_uuid IN (...)]` —
  every sync's meter load and every billing/metering read; the unique index leads with
  `user_uuid` so it cannot serve a period filter.
- The log tables' `ORDER BY created_at/started_at DESC LIMIT` report reads and the daily
  retention deletes ran over unindexed timestamps.
- Dunning's daily `payments WHERE status='pending'` scan (partial index — the table is
  dominated by settled rows).
- The financial-history ledger sorts `(period_label DESC, amount_toman DESC)` unindexed
  and grows forever by design.

Additive DDL only; instant at current sizes on both PostgreSQL and SQLite. (At much
larger sizes future index builds should move to CREATE INDEX CONCURRENTLY — recorded in
the remediation plan; at today's row counts a blocking build is milliseconds.)
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d7f3a9c1e5b8"
down_revision: str | None = "c4d8e2f6a1b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_enduser_panel_addedby", "end_user_snapshots", ["panel_id", "added_by_uuid"]
    )
    op.create_index(
        "ix_enduser_panel_addedby_lower",
        "end_user_snapshots",
        ["panel_id", sa.text("lower(added_by_uuid)")],
    )
    op.create_index(
        "ix_usage_meters_panel_period_addedby",
        "usage_meters",
        ["panel_id", "period_label", "added_by_uuid"],
    )
    op.create_index("ix_delivery_log_created_at", "delivery_log", ["created_at"])
    op.create_index(
        "ix_enforcement_actions_created_at", "enforcement_actions", ["created_at"]
    )
    op.create_index("ix_sync_runs_started_at", "sync_runs", ["started_at"])
    op.create_index(
        "ix_payments_pending",
        "payments",
        ["status"],
        postgresql_where=sa.text("status = 'pending'"),
        sqlite_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_finrec_period_amount",
        "financial_records",
        ["period_label", "amount_toman"],
    )


def downgrade() -> None:
    op.drop_index("ix_finrec_period_amount", table_name="financial_records")
    op.drop_index("ix_payments_pending", table_name="payments")
    op.drop_index("ix_sync_runs_started_at", table_name="sync_runs")
    op.drop_index("ix_enforcement_actions_created_at", table_name="enforcement_actions")
    op.drop_index("ix_delivery_log_created_at", table_name="delivery_log")
    op.drop_index("ix_usage_meters_panel_period_addedby", table_name="usage_meters")
    op.drop_index("ix_enduser_panel_addedby_lower", table_name="end_user_snapshots")
    op.drop_index("ix_enduser_panel_addedby", table_name="end_user_snapshots")
