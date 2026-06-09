"""Monthly invoice for a reseller, plus its per-service line items."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.enums import InvoiceStatus
from app.models.mixins import TimestampMixin


class Invoice(Base, TimestampMixin):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint(
            "reseller_id", "period_start", "period_end", name="uq_invoice_period"
        ),
        CheckConstraint("usage_gb >= 0", name="ck_invoices_usage_nonnegative"),
        CheckConstraint("price_per_gb >= 0", name="ck_invoices_price_nonnegative"),
        CheckConstraint("amount_toman >= 0", name="ck_invoices_toman_nonnegative"),
        CheckConstraint("base_amount_toman >= 0", name="ck_invoices_base_toman_nonnegative"),
        CheckConstraint("min_sale_toman >= 0", name="ck_invoices_min_sale_nonnegative"),
        CheckConstraint("usdt_rate >= 0", name="ck_invoices_rate_nonnegative"),
        CheckConstraint("amount_usdt >= 0", name="ck_invoices_usdt_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    reseller_id: Mapped[int] = mapped_column(
        ForeignKey("resellers.id", ondelete="CASCADE"), index=True
    )
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id"), index=True)

    # Billing window (Gregorian month)
    period_start: Mapped[dt.date] = mapped_column(Date)
    period_end: Mapped[dt.date] = mapped_column(Date)
    period_label: Mapped[str] = mapped_column(String(32), default="")  # e.g. "2026-02"

    # Computed figures
    usage_gb: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    users_count: Mapped[int] = mapped_column(Integer, default=0)
    price_per_gb: Mapped[int] = mapped_column(Integer, default=0)        # Toman
    amount_toman: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    # Amount before the minimum-sale floor; floor + flag for transparency on the PDF.
    base_amount_toman: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    min_sale_toman: Mapped[int] = mapped_column(Integer, default=0)
    floor_applied: Mapped[bool] = mapped_column(default=False)
    usdt_rate: Mapped[float] = mapped_column(Numeric(18, 2), default=0)  # Toman per USDT
    amount_usdt: Mapped[float] = mapped_column(Numeric(18, 6), default=0)

    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus, native_enum=False, length=16), default=InvoiceStatus.draft
    )
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Payment grace: while set and in the future, dunning/enforcement is paused for
    # this invoice (it stays owed). Lets the owner give a reseller more time without
    # affecting other invoices or panel data.
    deferred_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    defer_note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    pdf_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    reseller: Mapped["Reseller"] = relationship(back_populates="invoices")  # noqa: F821
    lines: Mapped[list["InvoiceLine"]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"
    __table_args__ = (
        CheckConstraint("usage_gb >= 0", name="ck_invoice_lines_usage_nonnegative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), index=True
    )
    end_user_uuid: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(255), default="")
    start_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    usage_gb: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    # Which sub-reseller created this service (for bundles).
    added_by_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sub_reseller_name: Mapped[str] = mapped_column(String(255), default="")

    invoice: Mapped["Invoice"] = relationship(back_populates="lines")
