"""storefront administration audit and idempotency

Revision ID: 1b84c0a7d3e5
Revises: a6c9e2f4b7d1
Create Date: 2026-07-16
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "1b84c0a7d3e5"
down_revision: str | None = "a6c9e2f4b7d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("storefront_bots", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("config_version", sa.Integer(), server_default=sa.text("1"), nullable=False)
        )
        batch_op.add_column(sa.Column("channel_verified_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("channel_verification_error", sa.String(32), nullable=True))

    bots = sa.table(
        "storefront_bots",
        sa.column("channel_id", sa.String(64)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("channel_verified_at", sa.DateTime(timezone=True)),
    )
    op.get_bind().execute(
        bots.update().where(bots.c.channel_id.is_not(None)).values(
            channel_verified_at=sa.func.coalesce(bots.c.updated_at, sa.func.now()))
    )

    op.create_table(
        "storefront_audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("storefront_bot_id", sa.Integer(), nullable=True),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_role", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=True),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("error_class", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("source in ('bot','portal','system')", name="ck_sfaudit_source"),
        sa.ForeignKeyConstraint(
            ["storefront_bot_id"], ["storefront_bots.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sfaudit_shop_created", "storefront_audit_events",
        ["storefront_bot_id", "created_at"], unique=False,
    )
    op.create_index(
        "ix_sfaudit_actor_action", "storefront_audit_events",
        ["actor_telegram_id", "action"], unique=False,
    )
    op.create_index(
        "ix_sfaudit_entity", "storefront_audit_events",
        ["entity_type", "entity_id"], unique=False,
    )

    op.create_table(
        "storefront_api_commands",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("storefront_bot_id", sa.Integer(), nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("external_io", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("error_class", sa.String(length=32), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status in ('pending','succeeded','failed','unknown')", name="ck_sfcommand_status"
        ),
        sa.ForeignKeyConstraint(
            ["storefront_bot_id"], ["storefront_bots.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "storefront_bot_id", "actor_telegram_id", "idempotency_key",
            name="uq_sfcommand_actor_key",
        ),
    )
    op.create_index(
        "ix_sfcommand_status_lease", "storefront_api_commands",
        ["status", "lease_expires_at"], unique=False,
    )
    op.create_index(
        "ix_sfcommand_shop_updated", "storefront_api_commands",
        ["storefront_bot_id", "updated_at"], unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_sfcommand_shop_updated", table_name="storefront_api_commands")
    op.drop_index("ix_sfcommand_status_lease", table_name="storefront_api_commands")
    op.drop_table("storefront_api_commands")
    op.drop_index("ix_sfaudit_entity", table_name="storefront_audit_events")
    op.drop_index("ix_sfaudit_actor_action", table_name="storefront_audit_events")
    op.drop_index("ix_sfaudit_shop_created", table_name="storefront_audit_events")
    op.drop_table("storefront_audit_events")
    with op.batch_alter_table("storefront_bots", schema=None) as batch_op:
        batch_op.drop_column("channel_verification_error")
        batch_op.drop_column("channel_verified_at")
        batch_op.drop_column("config_version")
