"""Owner / staff login accounts for the web panel."""
from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin


class AppUser(Base, TimestampMixin):
    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="owner")
    is_active: Mapped[bool] = mapped_column(default=True)
    # Bumped on every password change to invalidate previously-issued JWTs (the token carries
    # an `epoch` claim that must match this). 0 for legacy rows added before this column.
    token_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # Two-factor auth (TOTP / Google Authenticator). Secret is stored encrypted.
    totp_secret_enc: Mapped[str | None] = mapped_column(String(255), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(default=False)
