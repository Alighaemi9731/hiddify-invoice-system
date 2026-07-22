"""
Monthly usage-metering bucket per end-user — the data behind abuse-resistant billing.

The current-state running totals live on EndUserSnapshot (meter_provisioned_gb /
meter_consumed_gb). `UsageMeter` accumulates, per (panel, user, month), what was
provisioned and consumed and the extra the base "quota of users created this month"
rule would have missed:
  • overage_gb       — usage beyond the paid-for buffer (the daily-reset trick).
  • edit_renewal_gb  — quota topped up without updating start_date (renew-by-edit).
  • renew_used_gb    — consumption of cycles legitimately renewed away this month.
Billing adds (thresholded overage + edit_renewal + renew_used) on top of the normal
snapshot total. `UsageMeterEvent` keeps the per-event detail behind those aggregates
so invoices can name and enumerate each component («تمدید اول/دوم»، «افزایش حجم با
ویرایش»، «مصرف مازاد بر بسته») — see app.services.metering.
"""
from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin


class UsageMeter(Base, TimestampMixin):
    __tablename__ = "usage_meters"
    __table_args__ = (
        UniqueConstraint("panel_id", "user_uuid", "period_label", name="uq_usage_meter"),
        CheckConstraint("quota_added_gb >= 0", name="ck_usage_meters_quota_nonnegative"),
        CheckConstraint("consumed_gb >= 0", name="ck_usage_meters_consumed_nonnegative"),
        CheckConstraint("overage_gb >= 0", name="ck_usage_meters_overage_nonnegative"),
        CheckConstraint(
            "edit_renewal_gb >= 0", name="ck_usage_meters_edit_renewal_nonnegative"
        ),
        CheckConstraint(
            "renew_used_gb >= 0", name="ck_usage_meters_renew_used_nonnegative"
        ),
        CheckConstraint("reset_count >= 0", name="ck_usage_meters_reset_count_nonnegative"),
        # Every sync's meter load (panel_id, period_label) and every billing/metering
        # read (…, added_by_uuid IN …); the unique index leads with user_uuid and can't
        # serve a period filter.
        Index("ix_usage_meters_panel_period_addedby", "panel_id", "period_label", "added_by_uuid"),
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
    # Actual consumption of cycles that were legitimately renewed away (start_date advanced) this
    # month — capped at each closed cycle's sold quota. Billed so multiple in-month renewals aren't
    # under-counted, while an unused renewal isn't over-charged. See app.services.metering.
    renew_used_gb: Mapped[float] = mapped_column(
        Numeric(16, 3), default=0, server_default="0", nullable=False
    )
    reset_count: Mapped[int] = mapped_column(Integer, default=0)


class UsageMeterEvent(Base, TimestampMixin):
    """One row per DETECTED billable metering event — the per-event detail the aggregated
    UsageMeter row cannot keep. Written by the sync (metering.record_events) whenever a
    renewal banks the closed cycle's consumption (kind="renewal", gb=banked) or a quota
    top-up without a fresh start_date is caught (kind="edit_topup", gb=added). At billing
    time the events let the invoice enumerate each renewal separately («تمدید اول»،
    «تمدید دوم»، …) instead of one merged blob; when a month's events don't reconcile with
    the aggregate (legacy months, a missed sync write) billing falls back to the aggregate,
    so money is never derived from events — only presentation is. Append-only; `id` order
    is the in-month event order (detection is sync-granularity)."""
    __tablename__ = "usage_meter_events"
    __table_args__ = (
        Index("ix_usage_meter_events_lookup", "panel_id", "period_label", "user_uuid"),
        CheckConstraint("gb >= 0", name="ck_usage_meter_events_gb_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    panel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    user_uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    period_label: Mapped[str] = mapped_column(String(32), nullable=False)  # "2026-07"
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # "renewal" | "edit_topup"
    gb: Mapped[float] = mapped_column(Numeric(16, 3), nullable=False, default=0)
