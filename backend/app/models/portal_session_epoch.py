"""Per-Telegram-account portal session generation, so sessions can actually be revoked.

A portal session token is a stateless 30-day JWT that SLIDES (the client trades a valid one for a
fresh one while in use), and the only liveness check was "does *some* reseller row still carry this
bot_chat_id". So unbinding one panel from a multi-panel account revoked nothing, and there was no
way at all to say "log this account out everywhere".

Keyed on the CHAT ID rather than on `Reseller`, deliberately. A per-reseller-row epoch forces a bad
choice: taking the max across a caller's rows reproduces the bug (one untouched sibling row keeps
every session alive), while "any row moved" logs someone out of five panels because one was
unbound — and either way the counter would vanish with the row on delete, so re-binding would
resurrect old sessions. The chat id is what the token's subject actually is, and it outlives
reseller-row churn.

Backward compatible by construction: an absent row and a token with no `epoch` claim both read as
epoch 1, so existing sessions survive the deploy that introduces this.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, DateTime, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PortalSessionEpoch(Base):
    __tablename__ = "portal_session_epoch"

    # Telegram ids exceed int32.
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
