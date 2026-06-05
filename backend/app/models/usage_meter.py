"""
Monthly usage-metering bucket per end-user — the data behind abuse-resistant billing.

The current-state running totals live on EndUserSnapshot (meter_provisioned_gb /
meter_consumed_gb). This table accumulates, per (panel, user, month), what was
provisioned and consumed and — crucially — the ABNORMAL extra that the old
"quota of users created this month" rule would have missed:
  • overage_gb       — usage beyond the paid-for buffer (the daily-reset trick).
  • edit_renewal_gb  — quota topped up without updating start_date (renew-by-edit).
Billing adds (overage_gb + edit_renewal_gb) on top of the normal snapshot total.
"""
from __future__ import annotations


from sqlalchemy import Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin


class UsageMeter(Base, TimestampMixin):
    __tablename__ = "usage_meters"
    __table_args__ = (
        UniqueConstraint("panel_id", "user_uuid", "period_label", name="uq_usage_meter"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    panel_id: Mapped[int] = mapped_column(Integer, index=True)
    user_uuid: Mapped[str] = mapped_column(String(64), index=True)
    period_label: Mapped[str] = mapped_column(String(32), index=True)  # "2026-05"
    added_by_uuid: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    name: Mapped[str] = mapped_column(String(255), default="")

    quota_added_gb: Mapped[float] = mapped_column(Numeric(16, 3), default=0)   # new + top-ups
    consumed_gb: Mapped[float] = mapped_column(Numeric(16, 3), default=0)      # this month (reset-aware)
    overage_gb: Mapped[float] = mapped_column(Numeric(16, 3), default=0)       # beyond paid buffer
    edit_renewal_gb: Mapped[float] = mapped_column(Numeric(16, 3), default=0)  # top-up w/o new start_date
    reset_count: Mapped[int] = mapped_column(Integer, default=0)
