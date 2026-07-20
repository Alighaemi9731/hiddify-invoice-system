"""usage_meter_events — per-event metering detail for unambiguous invoice lines

The aggregated `usage_meters` row (one per panel/user/month) sums every renewal and
edit-top-up into single accumulator columns, so an invoice could only ever show one
merged «مصرف اضافه/تمدید» blob per user. This table records each DETECTED event —
`kind='renewal'` when a renewal banks the closed cycle's consumption (gb = the banked
amount) and `kind='edit_topup'` when a quota increase without a fresh start_date is
caught (gb = the added quota) — written by the sync alongside the meter update. Billing
then enumerates renewals separately («تمدید اول»، «تمدید دوم»، …); if a month's events
don't reconcile with the aggregate (months before this release, or a missed write) the
invoice falls back to the aggregate, so amounts are never derived from events.

Fully additive: a brand-new empty table; nothing existing changes.

Revision ID: c4d8e2f6a1b9
Revises: b7d2f9a3c1e5
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c4d8e2f6a1b9"
down_revision = "b7d2f9a3c1e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "usage_meter_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("panel_id", sa.Integer(), nullable=False),
        sa.Column("user_uuid", sa.String(length=64), nullable=False),
        sa.Column("period_label", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("gb", sa.Numeric(16, 3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.CheckConstraint("gb >= 0", name="ck_usage_meter_events_gb_nonnegative"),
    )
    op.create_index(
        "ix_usage_meter_events_lookup",
        "usage_meter_events",
        ["panel_id", "period_label", "user_uuid"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_meter_events_lookup", table_name="usage_meter_events")
    op.drop_table("usage_meter_events")
