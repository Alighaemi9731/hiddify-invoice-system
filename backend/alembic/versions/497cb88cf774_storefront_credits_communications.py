"""storefront credit archive + durable delivery queue (broadcast jobs + recipients)

Revision ID: 497cb88cf774
Revises: 525855401b89
Create Date: 2026-07-18

Plan 006. Additive: adds `archived_at` to credit codes (archiving sets enabled=False + stamps this,
preserving redemption history and the unique code — replaces the old hard delete), and the two
durable-delivery tables the storefront broadcast/direct-message worker uses.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "497cb88cf774"
down_revision: str | None = "525855401b89"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "storefront_credit_codes",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "storefront_broadcast_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("storefront_bot_id", sa.Integer(), nullable=False),
        sa.Column("actor_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("segment", sa.String(32), nullable=True),
        sa.Column("message_text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("total_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("sent_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("blocked_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("pending_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["storefront_bot_id"], ["storefront_bots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("kind in ('broadcast','direct')", name="ck_sfbjob_kind"),
        sa.CheckConstraint("status in ('queued','running','completed','canceled')",
                           name="ck_sfbjob_status"),
    )
    op.create_index("ix_storefront_broadcast_jobs_storefront_bot_id", "storefront_broadcast_jobs",
                    ["storefront_bot_id"])
    op.create_index("ix_sfbjob_shop_created", "storefront_broadcast_jobs",
                    ["storefront_bot_id", "created_at"])
    op.create_index("ix_sfbjob_created", "storefront_broadcast_jobs", ["created_at"])

    op.create_table(
        "storefront_delivery_recipients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_class", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["storefront_broadcast_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["storefront_customers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "customer_id", name="uq_sfdr_job_customer"),
        sa.CheckConstraint(
            "status in ('pending','sending','retry_wait','sent','blocked','failed','unknown','canceled')",
            name="ck_sfdr_status"),
    )
    op.create_index("ix_storefront_delivery_recipients_job_id", "storefront_delivery_recipients",
                    ["job_id"])
    op.create_index("ix_storefront_delivery_recipients_customer_id", "storefront_delivery_recipients",
                    ["customer_id"])
    op.create_index("ix_sfdr_claimable", "storefront_delivery_recipients",
                    ["status", "next_attempt_at", "lease_expires_at"])
    op.create_index("ix_sfdr_job_status", "storefront_delivery_recipients", ["job_id", "status"])
    op.create_index("ix_sfdr_created", "storefront_delivery_recipients", ["created_at"])


def downgrade() -> None:
    op.drop_table("storefront_delivery_recipients")
    op.drop_table("storefront_broadcast_jobs")
    op.drop_column("storefront_credit_codes", "archived_at")
