"""Separate financial issuance from notification and track registration deadlines."""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8d4f2a6c901"
down_revision: str | None = "e9b2c4d6f803"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("invoices") as batch:
        for name in ("issued_at", "collection_started_at", "delivery_attempted_at", "legacy_recovered_at"):
            batch.add_column(sa.Column(name, sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("telegram_delivery_status", sa.String(16), nullable=True))
    with op.batch_alter_table("resellers") as batch:
        batch.add_column(sa.Column("registration_deadline_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("registration_blocked", sa.Boolean(), nullable=False, server_default=sa.false()))
    # A late FIRST delivery already started the clock. Preserve it exactly, never use the
    # invoice's billing month or draft creation date in preference to the actual first send.
    op.execute("UPDATE invoices SET issued_at = sent_at, collection_started_at = sent_at, "
               "telegram_delivery_status = 'sent' WHERE sent_at IS NOT NULL AND status <> 'draft'")


def downgrade() -> None:
    with op.batch_alter_table("resellers") as batch:
        batch.drop_column("registration_blocked")
        batch.drop_column("registration_deadline_at")
    with op.batch_alter_table("invoices") as batch:
        for name in ("legacy_recovered_at", "telegram_delivery_status", "delivery_attempted_at",
                     "collection_started_at", "issued_at"):
            batch.drop_column(name)
