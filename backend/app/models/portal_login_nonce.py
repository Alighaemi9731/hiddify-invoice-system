"""Consumed one-time portal-login token ids (jti).

A portal login link is a short-lived signed JWT. To make it truly ONE-TIME (not just
expiry-bounded), each token carries a random `jti`; exchanging it inserts the jti here, and a
second exchange of the same token collides on the primary key and is rejected. Rows are pruned
once expired (the jti can never be replayed before then anyway, since the JWT itself expires)."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PortalLoginNonce(Base):
    __tablename__ = "portal_login_nonce"

    jti: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
