"""add panels.client_proxy_path_enc

Revision ID: a1c2e3f4b5d6
Revises: c3e6a9d24b71
Create Date: 2026-06-19

The end-user (client) secret path is DIFFERENT from the admin proxy path in Hiddify v12. We capture
it from the panel's hconfigs during sync to build customers' subscription links. Nullable + encrypted;
existing panels get it on their next sync, so there's no model/DB drift.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1c2e3f4b5d6"
down_revision: str | None = "c3e6a9d24b71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("panels") as batch_op:
        batch_op.add_column(
            sa.Column("client_proxy_path_enc", sa.String(length=512), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("panels") as batch_op:
        batch_op.drop_column("client_proxy_path_enc")
