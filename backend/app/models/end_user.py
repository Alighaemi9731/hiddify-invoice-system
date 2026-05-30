"""
Latest snapshot of a Hiddify end-user (VPN service), upserted on every panel sync.

We keep the freshest row per (panel, user_uuid). This is enough to:
  * compute invoices (start_date + usage_limit_GB + added_by_uuid), and
  * run enforcement (know which users to disable, and their prior `enable` state).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin


class EndUserSnapshot(Base, TimestampMixin):
    __tablename__ = "end_user_snapshots"
    __table_args__ = (
        UniqueConstraint("panel_id", "user_uuid", name="uq_enduser_panel_uuid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    panel_id: Mapped[int] = mapped_column(
        ForeignKey("panels.id", ondelete="CASCADE"), index=True
    )

    user_uuid: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    added_by_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    usage_limit_gb: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    current_usage_gb: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    start_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True, index=True)
    package_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    enable: Mapped[bool] = mapped_column(default=True)
    is_active: Mapped[bool] = mapped_column(default=True)
    mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_online: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_synced_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
