"""reseller_traffic_daily — per-reseller daily traffic history for the abuse audit

Nothing in the system recorded what a reseller ACTUALLY moved through the panel. Billing works on
sold quota, and `usage_meters` only sees `current_usage_GB` deltas between syncs, so a reseller
that resets its users' counters is invisible to both. On s7 one reseller pushed 9,647 GB in a
month against 1,100 GB of quota ever sold, and the highest `overage_gb` metering recorded for the
whole panel that month was 19 GB.

This table stores the panel's own accounting (Hiddify's `daily_usage`, read through
`GET /api/v2/admin/server_status/`) beside the quota that reseller sold, one row per day.

Denormalized with NO foreign keys, like `financial_records`: the reseller this history is about is
precisely the one likely to be deleted. The reseller that motivated the table was removed from the
panel days later, taking every trace with it — a cascade FK would do the same to our copy.

Purely additive: nothing reads this table before this release.

Revision ID: c9e4f7a2b810
Revises: b4d7e2f9a615
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c9e4f7a2b810"
down_revision = "b4d7e2f9a615"
branch_labels = None
depends_on = None

_TABLE = "reseller_traffic_daily"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("panel_key", sa.String(length=128), server_default="", nullable=False),
        # Soft reference only — no FK, so a deleted panel does not erase its history.
        sa.Column("panel_id", sa.Integer(), nullable=True),
        sa.Column(
            "reseller_admin_uuid", sa.String(length=64), server_default="", nullable=False
        ),
        sa.Column("reseller_name", sa.String(length=255), server_default="", nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        # What the PANEL thought the date was; `daily_usage` is keyed by the panel's own clock.
        sa.Column("panel_reported_day", sa.Date(), nullable=True),
        sa.Column("traffic_gb", sa.Numeric(16, 3), server_default="0", nullable=False),
        sa.Column("traffic_30d_gb", sa.Numeric(16, 3), server_default="0", nullable=False),
        sa.Column("online_users", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_users", sa.Integer(), server_default="0", nullable=False),
        sa.Column("quota_gb", sa.Numeric(16, 3), server_default="0", nullable=False),
        # The bundle's own counters — the reset evidence, free from the same query as the quota.
        sa.Column("counter_gb", sa.Numeric(16, 3), server_default="0", nullable=False),
        # NULL when quota_gb is 0 — there is no ceiling to divide by.
        sa.Column("ratio", sa.Numeric(12, 3), nullable=True),
        # sa.false(), not text("0"): a boolean server_default of "0" is rejected by Postgres.
        sa.Column("flagged", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("traffic_gb >= 0", name="ck_traffic_daily_traffic_nonnegative"),
        sa.CheckConstraint("traffic_30d_gb >= 0", name="ck_traffic_daily_traffic30_nonnegative"),
        sa.CheckConstraint("quota_gb >= 0", name="ck_traffic_daily_quota_nonnegative"),
        sa.CheckConstraint("counter_gb >= 0", name="ck_traffic_daily_counter_nonnegative"),
        sa.UniqueConstraint(
            "panel_key", "reseller_admin_uuid", "day", name="uq_reseller_traffic_daily"
        ),
    )
    op.create_index("ix_reseller_traffic_daily_panel_key", _TABLE, ["panel_key"])
    op.create_index(
        "ix_reseller_traffic_daily_reseller_admin_uuid", _TABLE, ["reseller_admin_uuid"]
    )
    op.create_index("ix_reseller_traffic_daily_day", _TABLE, ["day"])
    op.create_index("ix_reseller_traffic_daily_flagged", _TABLE, ["flagged"])
    # The report reads the newest day sorted by ratio DESC.
    op.create_index("ix_traffic_daily_day_ratio", _TABLE, ["day", "ratio"])


def downgrade() -> None:
    # Fully reversible: only the traffic audit reads this table, so dropping it returns the app
    # to "no traffic history", which is exactly the pre-revision state.
    op.drop_index("ix_traffic_daily_day_ratio", table_name=_TABLE)
    op.drop_index("ix_reseller_traffic_daily_flagged", table_name=_TABLE)
    op.drop_index("ix_reseller_traffic_daily_day", table_name=_TABLE)
    op.drop_index("ix_reseller_traffic_daily_reseller_admin_uuid", table_name=_TABLE)
    op.drop_index("ix_reseller_traffic_daily_panel_key", table_name=_TABLE)
    op.drop_table(_TABLE)
