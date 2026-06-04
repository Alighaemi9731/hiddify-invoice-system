"""
Durable financial ledger — a denormalized, self-describing snapshot of every
invoice's money facts (panel, reseller, month, amount, paid/unpaid).

Deliberately has NO foreign keys: the record survives a "wipe data / new panel"
reset and the deletion of the panel or reseller it came from, so the owner always
keeps a permanent history of who was billed how much, for which month, and whether
it was paid. Upserted by `app.services.financial_archive`.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Date, DateTime, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin


class FinancialRecord(Base, TimestampMixin):
    __tablename__ = "financial_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Soft reference to the source invoice (no FK → survives invoice deletion). Unique so a
    # given invoice has exactly one ledger row (enforced on fresh DBs; existing DBs are kept
    # de-duplicated by the self-healing collapse in financial_archive.record).
    invoice_id: Mapped[int | None] = mapped_column(Integer, unique=True, index=True, nullable=True)

    # Denormalized labels (kept even after panel/reseller removal).
    panel_key: Mapped[str] = mapped_column(String(128), default="", index=True)
    reseller_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    reseller_admin_uuid: Mapped[str] = mapped_column(String(64), default="", index=True)

    period_label: Mapped[str] = mapped_column(String(32), default="", index=True)
    period_start: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    usage_gb: Mapped[float] = mapped_column(Numeric(14, 3), default=0)
    price_per_gb: Mapped[int] = mapped_column(Integer, default=0)
    amount_toman: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    amount_usdt: Mapped[float] = mapped_column(Numeric(18, 6), default=0)

    status: Mapped[str] = mapped_column(String(16), default="", index=True)
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    txid: Mapped[str | None] = mapped_column(String(128), nullable=True)
