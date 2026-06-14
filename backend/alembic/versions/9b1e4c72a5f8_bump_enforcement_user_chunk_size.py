"""bump enforcement_user_chunk_size default from 100 to 500

Revision ID: 9b1e4c72a5f8
Revises: 3f2a7c91b8e4
Create Date: 2026-06-14

"""
from __future__ import annotations

from alembic import op

revision: str = "9b1e4c72a5f8"
down_revision: str | None = "3f2a7c91b8e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Only update installations that still carry the original default of 100.
    # Custom values (anything != '100') are left untouched.
    op.execute(
        "UPDATE settings SET value = '500' "
        "WHERE key = 'enforcement_user_chunk_size' AND value = '100'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE settings SET value = '100' "
        "WHERE key = 'enforcement_user_chunk_size' AND value = '500'"
    )
