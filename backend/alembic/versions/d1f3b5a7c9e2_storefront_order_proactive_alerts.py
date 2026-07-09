"""storefront_orders.trial_ended_alerted_at + usage_alerted_at — proactive-notice dedup stamps

Revision ID: d1f3b5a7c9e2
Revises: b7d9e1f3a5c2
Create Date: 2026-07-09

Two nullable timestamps that dedup the new proactive customer notices: the «free trial ended → buy
a plan» nudge (once per trial) and the «~80% of your volume used» warning (re-armed by a renewal).
No backfill — existing configs start un-alerted.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1f3b5a7c9e2"
down_revision: str | None = "b7d9e1f3a5c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "storefront_orders",
        sa.Column("trial_ended_alerted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "storefront_orders",
        sa.Column("usage_alerted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("storefront_orders") as batch_op:
        batch_op.drop_column("usage_alerted_at")
        batch_op.drop_column("trial_ended_alerted_at")
