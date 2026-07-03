"""widen delivery_log.tg_message_ids to Text; drop write-only invoices.pdf_path

Revision ID: e4f7b1c9a2d5
Revises: a8c5d7e2f4b6
Create Date: 2026-07-03

- `delivery_log.tg_message_ids` was `String(255)`. A large reseller subtree comma-joins one
  message id per delivered piece (invoice text + own PDF + one PDF per sub-reseller); 25+ subs
  overflow 255 chars on Postgres and fail the delivery-log write. Widen to Text.
- `invoices.pdf_path` was write-only (set on render, never read). Remove it.

Both use batch mode so they run on SQLite (tests/CI) as well as PostgreSQL (prod).
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e4f7b1c9a2d5"
down_revision: str | None = "a8c5d7e2f4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("delivery_log") as batch_op:
        batch_op.alter_column(
            "tg_message_ids",
            existing_type=sa.String(length=255),
            type_=sa.Text(),
            existing_nullable=True,
        )
    with op.batch_alter_table("invoices") as batch_op:
        batch_op.drop_column("pdf_path")


def downgrade() -> None:
    with op.batch_alter_table("invoices") as batch_op:
        batch_op.add_column(sa.Column("pdf_path", sa.String(length=512), nullable=True))
    with op.batch_alter_table("delivery_log") as batch_op:
        batch_op.alter_column(
            "tg_message_ids",
            existing_type=sa.Text(),
            type_=sa.String(length=255),
            existing_nullable=True,
        )
