"""Durable monthly billing progress.

Revision ID: d8a1f3b6c902
Revises: c9e4f7a2b810
"""
import sqlalchemy as sa

from alembic import op

revision = "d8a1f3b6c902"
down_revision = "c9e4f7a2b810"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monthly_billing_runs",
        sa.Column("period_start", sa.Date(), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("monthly_billing_runs")
