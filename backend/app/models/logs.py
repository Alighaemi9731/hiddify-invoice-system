"""Delivery log, enforcement actions, and panel sync runs (auditing / reporting)."""
from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.enums import (
    DeliveryKind,
    DeliveryStatus,
    EnforcementActionStatus,
    EnforcementActionType,
    SyncSource,
    SyncStatus,
)


class DeliveryLog(Base):
    """One row per attempt to deliver a message/invoice to a reseller via Telegram."""

    __tablename__ = "delivery_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    reseller_id: Mapped[int | None] = mapped_column(
        ForeignKey("resellers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[DeliveryKind] = mapped_column(
        Enum(DeliveryKind, native_enum=False, length=16), default=DeliveryKind.generic
    )
    channel: Mapped[str] = mapped_column(String(16), default="telegram")
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, native_enum=False, length=16)
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Telegram message id of a delivered invoice, so a resend can delete the old one.
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # All message ids of a multi-part invoice delivery (text + per-node PDFs), comma-joined,
    # so a resend can remove every old piece — not just the primary message.
    tg_message_ids: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )


class EnforcementAction(Base):
    """A combined suspension or restore attempt."""

    __tablename__ = "enforcement_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    reseller_id: Mapped[int] = mapped_column(
        ForeignKey("resellers.id", ondelete="CASCADE"), index=True
    )
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[EnforcementActionType] = mapped_column(
        Enum(EnforcementActionType, native_enum=False, length=16)
    )
    status: Mapped[EnforcementActionStatus] = mapped_column(
        Enum(EnforcementActionStatus, native_enum=False, length=16),
        default=EnforcementActionStatus.planned,
    )
    dry_run: Mapped[bool] = mapped_column(default=True)
    affected_count: Mapped[int] = mapped_column(Integer, default=0)
    # Snapshot of prior state (user enable flags, prior limits) for exact restore.
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )


class SyncRun(Base):
    """A panel data-sync attempt (for the Panels tab + freshness reporting)."""

    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    panel_id: Mapped[int | None] = mapped_column(
        ForeignKey("panels.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source: Mapped[SyncSource] = mapped_column(
        Enum(SyncSource, native_enum=False, length=16), default=SyncSource.backup_json
    )
    status: Mapped[SyncStatus] = mapped_column(
        Enum(SyncStatus, native_enum=False, length=16), default=SyncStatus.running
    )
    admin_count: Mapped[int] = mapped_column(Integer, default=0)
    user_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
