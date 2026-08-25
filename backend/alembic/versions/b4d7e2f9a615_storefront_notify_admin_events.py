"""storefront_bots.notify_admin_events — the shop's «اطلاع‌رسانی فروش» switch

Shop owners were never told when a customer bought or renewed: the only storefront events that ever
reached a shop admin were a wallet top-up submission and a provisioning FAILURE. The notifications
this column gates (purchase, renewal, confirmed top-up) are the point of the feature, so the column
defaults to TRUE — an existing shop starts receiving them without touching anything.

`server_default=sa.true()` is mandatory, not decoration: on Postgres a NOT NULL boolean `add_column`
over a populated table fails outright without one (it passes on SQLite, which is why this must be
validated against a real Postgres 16 — see CLAUDE.md).

Revision ID: b4d7e2f9a615
Revises: a7c1e9d3b5f2
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b4d7e2f9a615"
down_revision = "a7c1e9d3b5f2"
branch_labels = None
depends_on = None

_TABLE = "storefront_bots"
_COLUMN = "notify_admin_events"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
