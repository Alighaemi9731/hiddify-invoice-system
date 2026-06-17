"""add panels.host_aliases

Revision ID: c3e6a9d24b71
Revises: b2d5e8f1a673
Create Date: 2026-06-18

Old/alternate hosts (comma-separated, normalized) used ONLY to keep matching a reseller's stale
pasted link in the bot after a domain move. Nullable with a server default of '' so existing
panels are unaffected; the model declares the same default, so there's no model/DB drift.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3e6a9d24b71"
down_revision: str | None = "b2d5e8f1a673"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("panels") as batch_op:
        batch_op.add_column(
            sa.Column("host_aliases", sa.Text(), nullable=True, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("panels") as batch_op:
        batch_op.drop_column("host_aliases")
