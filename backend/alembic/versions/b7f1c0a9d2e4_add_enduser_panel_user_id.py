"""add end_user_snapshots.panel_user_id

Revision ID: b7f1c0a9d2e4
Revises: a1c2e3f4b5d6
Create Date: 2026-06-21

Hiddify's numeric primary-key id for a user, cached lazily during enforcement so suspend/restore can
build the bulk action's numeric rowids WITHOUT fetching the entire panel user list (which 503s on large
panels). Nullable: unknown until first resolved via the single-user API; populated + reused thereafter.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7f1c0a9d2e4"
down_revision: str | None = "a1c2e3f4b5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("end_user_snapshots") as batch_op:
        batch_op.add_column(sa.Column("panel_user_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("end_user_snapshots") as batch_op:
        batch_op.drop_column("panel_user_id")
