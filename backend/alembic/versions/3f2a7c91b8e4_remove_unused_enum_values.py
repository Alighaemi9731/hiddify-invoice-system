"""normalize obsolete enum values

Revision ID: 3f2a7c91b8e4
Revises: 6a9c7f21d4e0
Create Date: 2026-06-10
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3f2a7c91b8e4"
down_revision: str | None = "6a9c7f21d4e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_REPLACEMENTS = (
    ("panels", "source", ("admin_api", "sample"), "backup_json"),
    ("sync_runs", "source", ("admin_api", "sample"), "backup_json"),
    ("payments", "method", ("usdt_hd",), "usdt_txid"),
    ("payments", "status", ("duplicate",), "rejected"),
    ("delivery_log", "status", ("skipped",), "failed"),
    ("resellers", "enforcement_state", ("warned",), "active"),
    ("enforcement_actions", "action", ("warn", "zero_limits"), "disable_users"),
)


def upgrade() -> None:
    for table_name, column_name, old_values, new_value in _REPLACEMENTS:
        table = sa.table(table_name, sa.column(column_name, sa.String()))
        op.execute(
            table.update()
            .where(table.c[column_name].in_(old_values))
            .values({column_name: new_value})
        )


def downgrade() -> None:
    # Normalized values are all valid in the previous release. Reconstructing which obsolete
    # label a row used would be lossy, so no data rewrite is needed for rollback.
    pass
