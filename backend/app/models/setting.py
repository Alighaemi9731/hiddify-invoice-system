"""Runtime, panel-editable settings (key/value). See app.services.settings_service."""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.mixins import TimestampMixin


class Setting(Base, TimestampMixin):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[object] = mapped_column(JSON, nullable=True)
    # Secret values are stored encrypted; the API masks them on read.
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
