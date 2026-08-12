"""
Owner-side follow-up ("CRM lite") state for the reseller churn board.

Two tables, deliberately split the way every contact-management system splits them:

* `reseller_crm_state` — the CURRENT state, one row per reseller, created lazily on the
  first touch. It is what the board LEFT JOINs and filters on, so "hide the ones I already
  contacted" stays a single indexed join instead of a correlated MAX() over a growing log.
* `reseller_followups` — the append-only HISTORY. Denormalized (`panel_key`,
  `reseller_admin_uuid`, `reseller_name`) exactly like `FinancialRecord`, so the record of
  "I contacted this person" outlives the reseller row itself; a panel admin deleted upstream
  must not erase the fact that we chased them.

Note `ResellerCrmState.note` is NOT `Reseller.comment` — the latter is panel-sourced and
overwritten by every sync (`app.services.sync._upsert_resellers`), so it cannot hold an
owner's private note.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin


class ResellerCrmState(Base, TimestampMixin):
    """Per-reseller follow-up state. Absent row == never touched."""

    __tablename__ = "reseller_crm_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    reseller_id: Mapped[int] = mapped_column(
        ForeignKey("resellers.id", ondelete="CASCADE"), unique=True, index=True
    )

    # Hidden from the "due" view while this date is today or later. Expiry is by date only —
    # there is deliberately no auto-clear on segment change, so a snooze means exactly what
    # the owner typed.
    snoozed_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # Permanent "stop showing me this one" — outranks snoozed_until.
    muted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=func.false())

    last_touch_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    touch_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # The owner's own pinned note — survives sync, unlike Reseller.comment.
    note: Mapped[str] = mapped_column(Text, default="", server_default="")


class ResellerFollowup(Base, TimestampMixin):
    """One logged follow-up. Append-only; never updated, never pruned."""

    __tablename__ = "reseller_followups"
    __table_args__ = (
        # Per-reseller history, newest first (the drawer timeline).
        Index("ix_crmfollowup_reseller_created", "reseller_id", "created_at"),
        # The global paged log.
        Index("ix_crmfollowup_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # SET NULL, not CASCADE: a deleted reseller must not erase the outreach history.
    reseller_id: Mapped[int | None] = mapped_column(
        ForeignKey("resellers.id", ondelete="SET NULL"), nullable=True
    )

    # Denormalized identity, kept readable after the reseller row is gone.
    reseller_admin_uuid: Mapped[str] = mapped_column(String(64), default="", index=True)
    reseller_name: Mapped[str] = mapped_column(String(255), default="")
    panel_key: Mapped[str] = mapped_column(String(128), default="")

    # The segment the reseller was in at the moment of the touch — frozen, because the
    # board recomputes segments live and "why did I call them?" is otherwise unanswerable.
    segment: Mapped[str] = mapped_column(String(24), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    snoozed_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    muted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=func.false())
    actor: Mapped[str] = mapped_column(String(64), default="")
