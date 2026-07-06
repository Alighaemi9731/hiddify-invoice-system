"""storefront_bots.co_admin_ids — extra Telegram user-ids allowed to manage a shop bot

Revision ID: a3c5e7b9d1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-07-06

Adds a nullable TEXT `co_admin_ids` (comma-separated Telegram user-ids). A shop owner can appoint
co-managers who then reach the storefront admin panel from their own Telegram account, alongside the
owning reseller's `bot_chat_id`. No backfill (existing shops start with no co-admins).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a3c5e7b9d1f2"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "storefront_bots",
        sa.Column("co_admin_ids", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("storefront_bots") as batch_op:
        batch_op.drop_column("co_admin_ids")
