"""
Durable per-reseller traffic history — one row per top-level reseller per day, recording the
TRUE traffic the panel itself accounted for (Hiddify's own `daily_usage` table, read through
`GET /api/v2/admin/server_status/`) next to the quota that reseller had actually sold.

Deliberately has NO foreign keys, for the same reason as `financial_records`: the reseller that
this history is about is exactly the one likely to be deleted. The incident that motivated the
table («Mobile Fix», 2026-08) ended with the panel admin being removed, which erased every trace
of 9,647 GB of traffic. A cascade FK would delete the evidence at the moment it matters most.

Written by `app.services.traffic_audit`; pruned by the daily maintenance job at
`traffic_audit_retention_days`.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin


class ResellerTrafficDaily(Base, TimestampMixin):
    __tablename__ = "reseller_traffic_daily"
    __table_args__ = (
        # One row per reseller per day, so re-running a scan the same day (the manual button and
        # the cron job can overlap) upserts instead of duplicating the series.
        UniqueConstraint(
            "panel_key", "reseller_admin_uuid", "day", name="uq_reseller_traffic_daily"
        ),
        # The report sorts the newest day by ratio DESC.
        Index("ix_traffic_daily_day_ratio", "day", "ratio"),
        CheckConstraint("traffic_gb >= 0", name="ck_traffic_daily_traffic_nonnegative"),
        CheckConstraint("traffic_30d_gb >= 0", name="ck_traffic_daily_traffic30_nonnegative"),
        CheckConstraint("quota_gb >= 0", name="ck_traffic_daily_quota_nonnegative"),
        CheckConstraint("counter_gb >= 0", name="ck_traffic_daily_counter_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # Denormalized identity (kept even after the panel or the reseller is removed).
    panel_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    panel_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # soft ref, no FK
    reseller_admin_uuid: Mapped[str] = mapped_column(String(64), default="", index=True)
    reseller_name: Mapped[str] = mapped_column(String(255), default="")

    # `day` is OUR billing-local day (periods.today()); `panel_reported_day` is the date the panel
    # believed it was when it wrote the row. They normally match — s7 was once observed a full day
    # behind (NTP later corrected it), and `daily_usage` is keyed by the panel's own clock, so
    # keeping both makes a skewed panel visible instead of silently misaligning the series.
    day: Mapped[dt.date] = mapped_column(Date, index=True)
    panel_reported_day: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    # Traffic the PANEL accounted for — already rolled up over the reseller's whole sub-tree by
    # Hiddify's own `recursive_sub_admins_ids`, so sub-resellers are counted inside their parent.
    traffic_gb: Mapped[float] = mapped_column(Numeric(16, 3), default=0)  # yesterday
    traffic_30d_gb: Mapped[float] = mapped_column(Numeric(16, 3), default=0)
    online_users: Mapped[int] = mapped_column(Integer, default=0)  # active that day
    total_users: Mapped[int] = mapped_column(Integer, default=0)

    # The ceiling: quota this reseller actually sold. A user who bought 30 GB cannot legitimately
    # consume more, so traffic materially above this means counters are being reset.
    quota_gb: Mapped[float] = mapped_column(Numeric(16, 3), default=0)
    # Σ of the bundle's own `current_usage_gb`. Free (same query as the quota), and the single most
    # damning number in the report: «the panel logged 9,647 GB while their users' own counters sum
    # to 328 GB» is what actually proves a reset, where a ratio only suggests one.
    counter_gb: Mapped[float] = mapped_column(Numeric(16, 3), default=0)
    # NULL when quota_gb is 0 — there is no ceiling to divide by. NOT the same as "fine": a
    # reseller with real traffic and no quota at all is flagged on its own arm (see `is_flagged`).
    ratio: Mapped[float | None] = mapped_column(Numeric(12, 3), nullable=True)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
