"""PanelClient interface + the normalized data shapes parsed from a panel backup."""
from __future__ import annotations

import abc
import datetime as dt
import logging
import math
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("panel.parse")


def _to_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        f = float(value)
        if not math.isfinite(f):  # NaN/inf from a buggy panel must not reach the DB/billing
            return default
        return int(f)
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        f = float(value)
    except (TypeError, ValueError):
        return default
    # NaN/inf would silently corrupt billing: `round(sum(... nan ...))` is NaN and every
    # threshold comparison with NaN is False. Map any non-finite value to the default.
    return default if not math.isfinite(f) else f


def _to_date(value: Any) -> dt.date | None:
    if not value:
        return None
    if isinstance(value, dt.date):
        return value
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y %m %d"):
        try:
            return dt.datetime.strptime(str(value)[:10], fmt).date()
        except ValueError:
            continue
    return None


def _to_datetime(value: Any) -> dt.datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(str(value)[:19], fmt)
        except ValueError:
            continue
    return None


@dataclass
class PanelAdmin:
    uuid: str
    name: str
    parent_admin_uuid: str | None
    mode: str
    comment: str | None
    telegram_id: int | None
    max_users: int | None
    max_active_users: int | None
    can_add_admin: bool = False

    @property
    def is_owner(self) -> bool:
        return self.mode == "super_admin" or (self.name or "").strip().lower() == "owner"


@dataclass
class PanelUser:
    uuid: str
    name: str
    added_by_uuid: str | None
    start_date: dt.date | None
    usage_limit_gb: float
    current_usage_gb: float
    package_days: int | None
    enable: bool
    is_active: bool
    mode: str | None
    last_online: dt.datetime | None
    comment: str | None


@dataclass
class PanelData:
    admins: list[PanelAdmin] = field(default_factory=list)
    users: list[PanelUser] = field(default_factory=list)


def parse_backup(payload: dict) -> PanelData:
    """Normalize a Hiddify backup JSON document into PanelData.

    Defensive: a single malformed admin/user entry (or a non-dict/None container) is skipped
    rather than crashing the whole panel's sync."""
    if not isinstance(payload, dict):
        log.warning("parse_backup: payload is %s, not a dict — treating as empty", type(payload).__name__)
        return PanelData()

    admins: list[PanelAdmin] = []
    for a in (payload.get("admin_users") or []):
        if not isinstance(a, dict) or not a.get("uuid"):
            continue
        try:
            admins.append(PanelAdmin(
                uuid=a.get("uuid", ""),
                name=a.get("name") or "",
                parent_admin_uuid=a.get("parent_admin_uuid"),
                mode=a.get("mode") or "agent",
                comment=a.get("comment"),
                telegram_id=_to_int(a.get("telegram_id")),
                max_users=_to_int(a.get("max_users")),
                max_active_users=_to_int(a.get("max_active_users")),
                can_add_admin=bool(a.get("can_add_admin", False)),
            ))
        except Exception:  # noqa: BLE001 — skip a bad admin row, keep the rest
            log.warning("parse_backup: skipping malformed admin entry", exc_info=True)

    users: list[PanelUser] = []
    for u in (payload.get("users") or []):
        if not isinstance(u, dict) or not u.get("uuid"):
            continue
        try:
            users.append(PanelUser(
                uuid=u.get("uuid", ""),
                name=u.get("name") or "",
                added_by_uuid=u.get("added_by_uuid"),
                start_date=_to_date(u.get("start_date")),
                usage_limit_gb=_to_float(u.get("usage_limit_GB")),
                current_usage_gb=_to_float(u.get("current_usage_GB")),
                package_days=_to_int(u.get("package_days")),
                enable=bool(u.get("enable", True)),
                is_active=bool(u.get("is_active", True)),
                mode=u.get("mode"),
                last_online=_to_datetime(u.get("last_online")),
                comment=u.get("comment"),
            ))
        except Exception:  # noqa: BLE001 — skip a bad user row, keep the rest
            log.warning("parse_backup: skipping malformed user entry", exc_info=True)

    return PanelData(admins=admins, users=users)


class PanelClient(abc.ABC):
    """Abstraction over a single Hiddify panel."""

    # ---- read ----
    @abc.abstractmethod
    async def fetch_backup(self, panel) -> PanelData:  # noqa: ANN001
        ...

    # ---- write (enforcement) — implemented by the Admin-API adapter (M6) ----
    async def set_user_enabled(self, panel, user_uuid: str, enabled: bool) -> None:  # noqa: ANN001
        raise NotImplementedError

    async def set_admin_limits(
        self, panel, admin_uuid: str, max_users: int, max_active_users: int  # noqa: ANN001
    ) -> None:
        raise NotImplementedError
