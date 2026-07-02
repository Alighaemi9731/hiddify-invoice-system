"""Reseller payments (MVP: USDT BEP-20 via submitted TXID)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.enums import PaymentMethod, PaymentStatus
from app.models.mixins import TimestampMixin


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("confirmations >= 0", name="ck_payments_confirmations_nonnegative"),
        CheckConstraint("amount_usdt >= 0", name="ck_payments_usdt_nonnegative"),
        CheckConstraint(
            "amount_toman IS NULL OR amount_toman >= 0",
            name="ck_payments_toman_nonnegative",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    reseller_id: Mapped[int] = mapped_column(
        ForeignKey("resellers.id", ondelete="CASCADE"), index=True
    )
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True, index=True
    )

    method: Mapped[PaymentMethod] = mapped_column(
        Enum(PaymentMethod, native_enum=False, length=16), default=PaymentMethod.usdt_txid
    )
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, native_enum=False, length=16), default=PaymentStatus.pending
    )

    # On-chain details (BEP-20). txid is unique when present (prevents reuse).
    chain: Mapped[str] = mapped_column(String(16), default="bsc")
    txid: Mapped[str | None] = mapped_column(String(80), nullable=True, unique=True, index=True)
    from_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmations: Mapped[int] = mapped_column(Integer, default=0)

    amount_usdt: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    amount_toman: Mapped[float | None] = mapped_column(Numeric(18, 2), nullable=True)

    verified_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # raw chain response
    # Path to a deposit screenshot the reseller sent (method=screenshot), served to the
    # owner in the panel for manual confirmation. Relative to the backend working dir.
    proof_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Invoice ids settled by this payment. Kept comma-separated for compatibility with older
    # multi-invoice rows; current workflows store exactly one id.
    settled_invoice_ids: Mapped[str | None] = mapped_column(String(255), nullable=True)


class PaymentSettlement(Base):
    """One row per (payment, invoice) the payment covers/settles — the indexed, queryable
    mirror of `Payment.settled_invoice_ids` (I06). Dual-write: writers keep the comma column
    byte-equal for rollback safety; the hot lookups (duplicate-pending block, revert
    protection) query this table instead of loading every payment into Python."""

    __tablename__ = "payment_settlements"

    payment_id: Mapped[int] = mapped_column(
        ForeignKey("payments.id", ondelete="CASCADE"), primary_key=True
    )
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("invoices.id", ondelete="CASCADE"), primary_key=True, index=True
    )
