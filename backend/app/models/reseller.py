"""A reseller = a Hiddify admin (mode "agent"/"admin") under a panel's Owner."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models.enums import EnforcementState
from app.models.mixins import TimestampMixin


class Reseller(Base, TimestampMixin):
    __tablename__ = "resellers"
    __table_args__ = (
        UniqueConstraint("panel_id", "admin_uuid", name="uq_reseller_panel_uuid"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    panel_id: Mapped[int] = mapped_column(
        ForeignKey("panels.id", ondelete="CASCADE"), index=True
    )

    # Identity on the panel
    admin_uuid: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(255), default="")
    parent_admin_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(32), default="agent")  # agent/admin/super_admin
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_owner: Mapped[bool] = mapped_column(default=False)  # the panel super-admin

    # Telegram linkage
    panel_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # from panel
    bot_chat_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True
    )  # set when they register via the bot
    link_tag: Mapped[str | None] = mapped_column(String(255), nullable=True)  # the #fragment
    registered_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Billing
    price_per_gb: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Toman; NULL -> use the global default
    # Minimum billable amount for this reseller's whole bundle (Toman); NULL -> global.
    min_sale_toman: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exclude_from_billing: Mapped[bool] = mapped_column(default=False)

    # Latest values seen on the panel (refreshed each sync).
    panel_max_users: Mapped[int | None] = mapped_column(Integer, nullable=True)
    panel_max_active_users: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Enforcement state machine
    enforcement_state: Mapped[EnforcementState] = mapped_column(
        Enum(EnforcementState, native_enum=False, length=16),
        default=EnforcementState.active,
    )
    # Values captured right before enforcement, used to restore exactly on payment.
    max_users_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_active_users_snapshot: Mapped[int | None] = mapped_column(Integer, nullable=True)

    last_seen_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    panel: Mapped["Panel"] = relationship(back_populates="resellers")  # noqa: F821
    invoices: Mapped[list["Invoice"]] = relationship(  # noqa: F821
        back_populates="reseller", cascade="all, delete-orphan"
    )
