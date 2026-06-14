"""bump enforcement_user_chunk_size default from 100 to 500

Revision ID: 9b1e4c72a5f8
Revises: 3f2a7c91b8e4
Create Date: 2026-06-14

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "9b1e4c72a5f8"
down_revision: str | None = "3f2a7c91b8e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The settings.value column is JSON in PostgreSQL and TEXT in SQLite (tests).
    # Use dialect-aware SQL to avoid "operator does not exist: json = unknown" on PG.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text(
            "UPDATE settings SET value = '500'::json "
            "WHERE key = 'enforcement_user_chunk_size' AND value::text = '100'"
        ))
    else:
        bind.execute(sa.text(
            "UPDATE settings SET value = '500' "
            "WHERE key = 'enforcement_user_chunk_size' AND value = '100'"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text(
            "UPDATE settings SET value = '100'::json "
            "WHERE key = 'enforcement_user_chunk_size' AND value::text = '500'"
        ))
    else:
        bind.execute(sa.text(
            "UPDATE settings SET value = '100' "
            "WHERE key = 'enforcement_user_chunk_size' AND value = '500'"
        ))
