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
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin


class EndUserSnapshot(Base, TimestampMixin):
    __tablename__ = "end_user_snapshots"
    __table_args__ = (
        UniqueConstraint("panel_id", "user_uuid", name="uq_enduser_panel_uuid"),
        # Hot filter of the whole app: WHERE panel_id=? AND added_by_uuid IN (...)
        # (reports/portal/capacity/invoice engine) plus the lower() variant used by
        # enforcement/reseller counts, which a plain b-tree can't serve.
        Index("ix_enduser_panel_addedby", "panel_id", "added_by_uuid"),
        Index("ix_enduser_panel_addedby_lower", "panel_id", text("lower(added_by_uuid)")),
        # Billing asks for one month at a time: `panel_id = ? AND start_date BETWEEN ? AND ?`
        # (app/services/invoicing._period_users_q). Leading with panel_id matters — with ~10
        # panels that column is barely selective, so the range on start_date is what narrows
        # the scan to the ~8% of rows created in the period.
        Index("ix_enduser_panel_start_date", "panel_id", "start_date"),
        # NOTE — deliberately NOT indexed for the owner's end-user search. The 2026-08 audit
        # proposed a pg_trgm GIN index on `name` for its unanchored `ILIKE '%q%'`, then measured
        # the premise away: on 200k rows the whole search is a 45 ms parallel seq scan, not the
        # 200 ms–1 s the audit assumed. A GIN index would add write amplification to the most
        # heavily UPSERTed table in the system (every user of every panel, every sync) to save
        # ~40 ms on an occasional manual lookup. Revisit only if the table grows an order of
        # magnitude or the search becomes type-ahead.
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    panel_id: Mapped[int] = mapped_column(
        ForeignKey("panels.id", ondelete="CASCADE"), index=True
    )

    user_uuid: Mapped[str] = mapped_column(String(64), index=True)
    # Hiddify's numeric primary-key id for this user, cached lazily during enforcement (the bulk
    # enable/disable action needs numeric rowids). Lets us avoid the heavy whole-panel user-list
    # fetch — see app.services.enforcement. Nullable: unknown until first resolved.
    panel_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
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

    # --- Metering running state (lifetime, reset-aware) — see app.services.metering ---
    # provisioned = total GB ever sold/topped-up; consumed = true cumulative usage
    # (survives the reseller resetting current_usage). The remaining buffer
    # (provisioned - consumed) is what the reseller has legitimately paid for; usage
    # beyond it is "overage" (billed as abuse). `meter_init` guards baseline setup so a
    # pre-existing user is not re-billed when metering is first switched on.
    meter_provisioned_gb: Mapped[float] = mapped_column(Numeric(16, 3), default=0)
    meter_consumed_gb: Mapped[float] = mapped_column(Numeric(16, 3), default=0)
    meter_init: Mapped[bool] = mapped_column(default=False)
