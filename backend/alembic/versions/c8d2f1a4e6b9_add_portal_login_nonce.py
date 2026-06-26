"""add portal_login_nonce (one-time portal login links)

Revision ID: c8d2f1a4e6b9
Revises: b7f1c0a9d2e4
Create Date: 2026-06-26

Tracks consumed portal-login token ids (jti) so a Telegram login link can be exchanged exactly
once. Without it the stateless login JWT could be replayed repeatedly within its 15-minute TTL to
mint multiple reseller sessions.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8d2f1a4e6b9"
down_revision: str | None = "b7f1c0a9d2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "portal_login_nonce",
        sa.Column("jti", sa.String(length=64), primary_key=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_portal_login_nonce_expires_at", "portal_login_nonce", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_portal_login_nonce_expires_at", table_name="portal_login_nonce")
    op.drop_table("portal_login_nonce")
