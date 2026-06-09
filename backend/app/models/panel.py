"""A Hiddify panel the owner controls (up to ~10)."""
from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core import crypto
from app.core.db import Base
from app.models.enums import PanelStatus, SyncSource
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.reseller import Reseller


class Panel(Base, TimestampMixin):
    __tablename__ = "panels"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # e.g. "fa1"
    name: Mapped[str] = mapped_column(String(128), default="")

    # Connection details. `proxy_path` and `admin_api_key` are stored encrypted.
    host: Mapped[str] = mapped_column(String(255))            # e.g. panel-01.example.com
    proxy_path_enc: Mapped[str] = mapped_column(String(512))  # the secret URL path
    owner_uuid: Mapped[str] = mapped_column(String(64))       # panel super-admin uuid
    admin_api_key_enc: Mapped[str | None] = mapped_column(String(512), nullable=True)

    enabled: Mapped[bool] = mapped_column(default=True)
    status: Mapped[PanelStatus] = mapped_column(
        Enum(PanelStatus, native_enum=False, length=16), default=PanelStatus.unknown
    )
    source: Mapped[SyncSource] = mapped_column(
        Enum(SyncSource, native_enum=False, length=16), default=SyncSource.backup_json
    )
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    resellers: Mapped[list[Reseller]] = relationship(
        back_populates="panel", cascade="all, delete-orphan"
    )

    # ---- secret accessors (transparent encryption) ----
    @property
    def proxy_path(self) -> str | None:
        return crypto.decrypt(self.proxy_path_enc)

    @proxy_path.setter
    def proxy_path(self, value: str | None) -> None:
        self.proxy_path_enc = crypto.encrypt(value) or ""

    @property
    def admin_api_key(self) -> str | None:
        return crypto.decrypt(self.admin_api_key_enc)

    @admin_api_key.setter
    def admin_api_key(self, value: str | None) -> None:
        self.admin_api_key_enc = crypto.encrypt(value)

    # ---- derived URLs ----
    @property
    def proxy_base(self) -> str:
        return f"https://{self.host}/{self.proxy_path}"

    @property
    def base_secret_url(self) -> str:
        return f"{self.proxy_base}/{self.owner_uuid}"

    @property
    def backup_url(self) -> str:
        # Hiddify authenticates this endpoint via the owner uuid as the HTTP
        # basic-auth username, with the uuid removed from the path.
        return f"{self.proxy_base}/admin/backup/backupfile/"

    @property
    def admin_api_base(self) -> str:
        # The Hiddify v2 admin API lives under the proxy path (NOT the uuid path);
        # auth is the Hiddify-API-Key header (the admin uuid).
        return f"{self.proxy_base}/api/v2/admin"
