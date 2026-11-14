"""Explicit panel tombstones and per-panel maintenance.

Revision ID: e9b2c4d6f803
Revises: d8a1f3b6c902
"""
import sqlalchemy as sa

from alembic import op

revision = "e9b2c4d6f803"
down_revision = "d8a1f3b6c902"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("end_user_snapshots", sa.Column(
        "panel_deleted", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("panels", sa.Column(
        "maintenance", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("panels", "maintenance")
    op.drop_column("end_user_snapshots", "panel_deleted")
