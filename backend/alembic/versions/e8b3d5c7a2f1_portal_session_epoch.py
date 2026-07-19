"""portal_session_epoch — revocable reseller portal sessions

A portal session is a stateless 30-day JWT that slides indefinitely, and the only liveness check
was "does some reseller row still carry this bot_chat_id". So there was no way to end a session:
unbinding one panel of a multi-panel account revoked nothing, and a captured Telegram login payload
could be replayed into a fresh session. This table records a per-Telegram-account generation that
the token carries and every request re-checks, so a bump invalidates every session at once —
including the sliding-refresh chain.

Keyed on chat_id rather than reseller_id on purpose: the token's subject IS the chat id, and the
counter must outlive reseller-row churn (a per-row epoch would vanish on delete and let re-binding
resurrect old sessions).

Additive and backward compatible: an absent row and a token without the claim both read as epoch 1,
so this deploy logs nobody out.

Revision ID: e8b3d5c7a2f1
Revises: d4f7a2b9c1e8
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e8b3d5c7a2f1"
down_revision = "d4f7a2b9c1e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portal_session_epoch",
        sa.Column("chat_id", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("epoch", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    # Fully reversible: dropping the table returns every account to the implicit epoch 1, which is
    # exactly what tokens minted before this revision carry.
    op.drop_table("portal_session_epoch")
