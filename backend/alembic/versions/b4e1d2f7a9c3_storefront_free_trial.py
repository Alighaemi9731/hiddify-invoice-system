"""storefront free trial — per-bot config + per-customer used flag

Revision ID: b4e1d2f7a9c3
Revises: a3f5c9e1b7d2
Create Date: 2026-06-27

Adds a one-time free trial: storefront_bots gains free_trial_enabled/gb/days (admin opt-in, default
1 GB · 1 day) and storefront_customers gains free_trial_used. Booleans use server_default=sa.false()
(Postgres rejects DEFAULT 0 for BOOLEAN); integers default to 1. Existing rows backfill from the
server defaults.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b4e1d2f7a9c3"
down_revision: str | None = "a3f5c9e1b7d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("storefront_bots", sa.Column(
        "free_trial_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("storefront_bots", sa.Column(
        "free_trial_gb", sa.Integer(), nullable=False, server_default=sa.text("1")))
    op.add_column("storefront_bots", sa.Column(
        "free_trial_days", sa.Integer(), nullable=False, server_default=sa.text("1")))
    op.add_column("storefront_customers", sa.Column(
        "free_trial_used", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("storefront_customers", "free_trial_used")
    op.drop_column("storefront_bots", "free_trial_days")
    op.drop_column("storefront_bots", "free_trial_gb")
    op.drop_column("storefront_bots", "free_trial_enabled")
