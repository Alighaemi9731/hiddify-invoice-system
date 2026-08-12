"""reseller_crm_state + reseller_followups — the reseller follow-up / churn board

The owner tracks ~400 top-level resellers by hand: who never created a user, who stopped
creating them, who is suspended. The signals all existed (invoices, snapshots, enforcement
state) but nothing remembered "I already contacted this one", so every pass re-surfaced the
same people. These two tables add that memory.

`reseller_crm_state` is the current state (one row per reseller, created on the first touch)
— the board LEFT JOINs it to hide snoozed/muted rows. `reseller_followups` is the append-only
history, denormalized like `financial_records` so the record of an outreach outlives the
reseller row: a panel admin deleted upstream must not erase the fact that we chased them,
hence SET NULL rather than CASCADE on its FK.

Purely additive — nothing reads these tables before this release, so the upgrade is a no-op
for every existing behaviour.

Revision ID: b3f6a1d94c27
Revises: a4e7c2b9f1d6
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b3f6a1d94c27"
down_revision = "a4e7c2b9f1d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reseller_crm_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reseller_id",
            sa.Integer(),
            sa.ForeignKey("resellers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snoozed_until", sa.Date(), nullable=True),
        # sa.false(), not text("0"): a boolean server_default of "0" is rejected by Postgres.
        sa.Column("muted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("last_touch_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("touch_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Unique: the state row is 1:1 with the reseller, upserted on every touch.
    op.create_index(
        "ix_reseller_crm_state_reseller_id", "reseller_crm_state", ["reseller_id"], unique=True
    )

    op.create_table(
        "reseller_followups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "reseller_id",
            sa.Integer(),
            sa.ForeignKey("resellers.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reseller_admin_uuid", sa.String(length=64), server_default="", nullable=False),
        sa.Column("reseller_name", sa.String(length=255), server_default="", nullable=False),
        sa.Column("panel_key", sa.String(length=128), server_default="", nullable=False),
        sa.Column("segment", sa.String(length=24), server_default="", nullable=False),
        sa.Column("note", sa.Text(), server_default="", nullable=False),
        sa.Column("snoozed_until", sa.Date(), nullable=True),
        sa.Column("muted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("actor", sa.String(length=64), server_default="", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_reseller_followups_reseller_admin_uuid",
        "reseller_followups",
        ["reseller_admin_uuid"],
    )
    # The drawer timeline (per reseller, newest first).
    op.create_index(
        "ix_crmfollowup_reseller_created", "reseller_followups", ["reseller_id", "created_at"]
    )
    # The global paged log.
    op.create_index("ix_crmfollowup_created", "reseller_followups", ["created_at"])


def downgrade() -> None:
    # Fully reversible: nothing outside the follow-up board reads these tables, so dropping
    # them returns the app to "no follow-up memory", which is exactly the pre-revision state.
    op.drop_index("ix_crmfollowup_created", table_name="reseller_followups")
    op.drop_index("ix_crmfollowup_reseller_created", table_name="reseller_followups")
    op.drop_index("ix_reseller_followups_reseller_admin_uuid", table_name="reseller_followups")
    op.drop_table("reseller_followups")
    op.drop_index("ix_reseller_crm_state_reseller_id", table_name="reseller_crm_state")
    op.drop_table("reseller_crm_state")
