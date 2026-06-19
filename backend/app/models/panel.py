"""A Hiddify panel the owner controls (up to ~10)."""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core import crypto
from app.core.db import Base
from app.models.enums import PanelStatus, SyncSource
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.reseller import Reseller


def _name_slug(name: str) -> str:
    """Approximate Hiddify's `unicode_slug` (slugify(name, lowercase=False, allow_unicode=True)) for
    the subscription link's #fragment: keep unicode letters/digits, turn spaces/separators into a
    single hyphen, strip the rest. The fragment is a cosmetic label (not sent to the server), so an
    exact byte-match isn't required, but this matches normal names ("milad store33" → "milad-store33",
    "تکی اول تست" → "تکی-اول-تست")."""
    s = unicodedata.normalize("NFKC", name or "").strip()
    s = re.sub(r"[\s_]+", "-", s)            # whitespace / underscores → hyphen
    s = re.sub(r"[^\w\-]", "", s)            # drop anything that isn't a word char or hyphen (unicode)
    s = re.sub(r"-{2,}", "-", s).strip("-")  # collapse + trim hyphens
    return s


class Panel(Base, TimestampMixin):
    __tablename__ = "panels"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # e.g. "fa1"
    name: Mapped[str] = mapped_column(String(128), default="")

    # Connection details. `proxy_path` and `admin_api_key` are stored encrypted.
    host: Mapped[str] = mapped_column(String(255))            # e.g. panel-01.example.com
    proxy_path_enc: Mapped[str] = mapped_column(String(512))  # the ADMIN secret URL path
    # The CLIENT (end-user) secret path — DIFFERENT from the admin path in Hiddify v12. Captured from
    # the panel's hconfigs during sync; used to build customers' subscription links. Nullable.
    client_proxy_path_enc: Mapped[str | None] = mapped_column(String(512), nullable=True)
    owner_uuid: Mapped[str] = mapped_column(String(64))       # panel super-admin uuid
    admin_api_key_enc: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Old/alternate hosts (comma-separated, normalized). ONLY used to keep matching a reseller's
    # stale pasted link in the bot after a domain move — never touches billing, backup, or the
    # Admin API (those always derive from `host`).
    host_aliases: Mapped[str] = mapped_column(Text, nullable=True, default="", server_default="")

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
    def client_proxy_path(self) -> str | None:
        return crypto.decrypt(self.client_proxy_path_enc) if self.client_proxy_path_enc else None

    @client_proxy_path.setter
    def client_proxy_path(self, value: str | None) -> None:
        self.client_proxy_path_enc = crypto.encrypt(value) if value else None

    @property
    def admin_api_key(self) -> str | None:
        return crypto.decrypt(self.admin_api_key_enc)

    @admin_api_key.setter
    def admin_api_key(self, value: str | None) -> None:
        self.admin_api_key_enc = crypto.encrypt(value)

    # ---- host aliases (matcher-only; never affect fetch) ----
    @property
    def host_alias_list(self) -> list[str]:
        return [h.strip() for h in (self.host_aliases or "").split(",") if h.strip()]

    def set_host_aliases(self, values: list[str] | None) -> None:
        """Normalize, de-dup, drop blanks and any entry equal to the current host, then store."""
        from app.bot.matching import normalize_host

        main = normalize_host(self.host)
        seen: list[str] = []
        for value in values or []:
            n = normalize_host(value)
            if not n or n == main or n in seen:
                continue
            seen.append(n)
        self.host_aliases = ",".join(seen)

    def admin_link(self, admin_uuid: str, *, tag: str | None = None, host: str | None = None) -> str:
        """The reseller's own Hiddify admin URL: https://<host>/<proxy_path>/<uuid>/[#tag].
        `host` defaults to the panel's current host (so links auto-update after a domain move)."""
        base = f"https://{host or self.host}/{self.proxy_path}/{admin_uuid}/"
        return base + (f"#{tag}" if tag else "")

    def user_sub_link(self, user_uuid: str, *, name: str | None = None) -> str:
        """An end-user's subscription URL exactly as the Hiddify panel shares it:
        https://<host>/<CLIENT_proxy_path>/<uuid>/[#<name-slug>]. The client path differs from the
        admin `proxy_path` in v12; fall back to the admin path only if it hasn't been captured yet."""
        path = self.client_proxy_path or self.proxy_path
        base = f"https://{self.host}/{path}/{user_uuid}/"
        return base + (f"#{_name_slug(name)}" if name else "")

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
