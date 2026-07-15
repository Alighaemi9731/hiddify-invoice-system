"""storefront renewal recovery target

Revision ID: a6c9e2f4b7d1
Revises: 7968884fecbd
Create Date: 2026-07-15

Persist the absolute Hiddify renewal target before charging the wallet. A crashed worker can then
verify or idempotently reapply the remote mutation instead of blindly reversing a successful renewal.
Both columns are nullable so legacy in-flight operations retain the conservative recovery path.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a6c9e2f4b7d1"
down_revision: str | None = "7968884fecbd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("storefront_operations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("target_usage_limit_gb", sa.Numeric(12, 3), nullable=True))
        batch_op.add_column(sa.Column("prior_panel_start_date", sa.String(32), nullable=True))

    # Keep an existing install's visible setting aligned with the new effective safety floor. Use a
    # typed SQLAlchemy update so JSON serialization is portable across SQLite tests and PostgreSQL.
    settings = sa.table(
        "settings", sa.column("key", sa.String(64)), sa.column("value", sa.JSON()))
    bind = op.get_bind()
    stored = bind.execute(
        sa.select(settings.c.value).where(
            settings.c.key == "storefront_operation_lease_seconds")
    ).scalar_one_or_none()
    try:
        too_short = int(stored) < 300
    except (TypeError, ValueError):
        too_short = True
    if too_short:
        bind.execute(
            settings.update()
            .where(settings.c.key == "storefront_operation_lease_seconds")
            .values(value=300)
        )


def downgrade() -> None:
    with op.batch_alter_table("storefront_operations", schema=None) as batch_op:
        batch_op.drop_column("prior_panel_start_date")
        batch_op.drop_column("target_usage_limit_gb")
    # Do not lower the runtime setting on downgrade; 300 remains safe for the previous code too.
