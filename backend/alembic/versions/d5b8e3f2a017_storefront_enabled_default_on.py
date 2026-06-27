"""storefront enabled by default for everyone — backfill existing resellers

Revision ID: d5b8e3f2a017
Revises: c7a2f4e9d1b6
Create Date: 2026-06-27

The storefront permission (`resellers.storefront_enabled`) is now ON by default for every reseller (the
owner no longer toggles it one-by-one; only top-level resellers can actually set one up, enforced by the
bot's setup gate). New resellers default to enabled via the model; this backfills all existing rows.
Data-only (no DDL); the boolean literal is rendered per-dialect by SQLAlchemy core (PG `true`, not `0`).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5b8e3f2a017"
down_revision: str | None = "c7a2f4e9d1b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    resellers = sa.table("resellers", sa.column("storefront_enabled", sa.Boolean()))
    op.execute(resellers.update().values(storefront_enabled=True))


def downgrade() -> None:
    # No-op: we can't tell which rows were enabled before the backfill, and leaving them enabled is
    # harmless (only top-level resellers can use it, and the fee defaults to free).
    pass
