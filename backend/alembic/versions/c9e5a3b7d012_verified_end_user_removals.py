"""Keep verified operator removals across Hiddify soft-delete backups."""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9e5a3b7d012"
down_revision: str | None = "b8d4f2a6c901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "end_user_removals",
        sa.Column("panel_id", sa.Integer(), sa.ForeignKey("panels.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_uuid", sa.String(64), primary_key=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("end_user_removals")
