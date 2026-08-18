"""The OWNER's own one-tap test config («🧪 کانفیگ تست» in the admin bot).

A new customer asks for a trial before buying; this makes one in a single tap — same shape as the
storefront bots' «🎁 تست رایگان», but for the owner personally and without any order/wallet/billing
machinery around it.

Two things here are deliberate:

* The config is created AS the panel's own super-admin (`api_key = panel.owner_uuid`), so its
  `added_by_uuid` is the Owner row. `invoice_engine.select_billable_roots` skips `is_owner` rows, so
  the quota lands in NOBODY's invoice — the owner funds their own tests, exactly like the storefront
  trial. Passing `api_key=None` would let the header builder fall back to `panel.admin_api_key`,
  which may be a different admin entirely (see `enforcement._set_admin_passwords`) — and then a
  reseller would silently be billed for the owner's test.
* The panel is an explicit setting, never a guess. `test_config_panel_id = 0` means "not chosen
  yet"; the bot asks once and remembers. The single-enabled-panel shortcut is still deterministic
  (there is nothing to choose between), not a random pick.

Nothing is persisted: the config shows up under the Owner row on the next sync and expires on its
own after `test_config_days`.
"""
from __future__ import annotations

import logging
import uuid as uuidlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Panel
from app.services import settings_service, usercreate
from app.services.panel_client.admin_api import AdminApiClient, UserLimitError

log = logging.getLogger("testconfig")

_PANEL_KEY = "test_config_panel_id"
_GB_KEY = "test_config_gb"
_DAY_KEY = "test_config_days"
_NAME_KEY = "test_config_name"

_DEFAULT_NAME = "test"


@dataclass
class TestOptions:
    panel_id: int
    gb: int
    days: int
    name: str


@dataclass
class TestResult:
    ok: bool
    sub_link: str | None = None
    uuid: str | None = None
    name: str = ""
    gb: int = 0
    days: int = 0
    reason: str | None = None   # "limit" | "no_admin" | "error"


def _int(value: object, fallback: int) -> int:
    """A settings value that isn't a usable positive number falls back to the shipped default."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return fallback
    try:
        n = int(value)
    except (TypeError, ValueError):
        return fallback
    return n if n > 0 else fallback


async def load_options(session: AsyncSession) -> TestOptions:
    cfg = await settings_service.get_many(session, [_PANEL_KEY, _GB_KEY, _DAY_KEY, _NAME_KEY])
    try:
        panel_id = int(cfg.get(_PANEL_KEY) or 0)
    except (TypeError, ValueError):
        panel_id = 0
    name = str(cfg.get(_NAME_KEY) or "").strip() or _DEFAULT_NAME
    return TestOptions(
        panel_id=max(0, panel_id),
        gb=_int(cfg.get(_GB_KEY), 2),
        days=_int(cfg.get(_DAY_KEY), 2),
        name=name[:40],
    )


async def panel_choices(session: AsyncSession) -> list[tuple[int, str]]:
    """The enabled panels the owner may pick from: (id, label), ordered by key."""
    panels = (
        await session.execute(
            select(Panel).where(Panel.enabled.is_(True)).order_by(Panel.key)
        )
    ).scalars().all()
    return [(p.id, f"{p.key} — {p.name}" if p.name else p.key) for p in panels]


async def resolve_panel(session: AsyncSession) -> tuple[Panel | None, str]:
    """(panel, reason) — "ok" | "unset" (ask the owner) | "missing" (the saved panel is gone).

    A saved panel that was deleted or disabled must NOT silently fall back to another one: the whole
    point of the setting is that the owner knows which panel their customer's test comes from."""
    opts = await load_options(session)
    if opts.panel_id:
        panel = await session.get(Panel, opts.panel_id)
        if panel is not None and panel.enabled:
            return panel, "ok"
        return None, "missing"
    panels = (
        await session.execute(
            select(Panel).where(Panel.enabled.is_(True)).order_by(Panel.key)
        )
    ).scalars().all()
    if len(panels) == 1:
        return panels[0], "ok"  # nothing to choose between — still not a random pick
    return None, "unset"


async def set_panel(session: AsyncSession, panel_id: int) -> None:
    await settings_service.set_value(session, _PANEL_KEY, int(panel_id))


async def create(
    session: AsyncSession, panel: Panel, *, gb: int, days: int, name: str
) -> TestResult:
    """Create ONE test config on `panel` as its super-admin and return its subscription link."""
    admin_uuid = (panel.owner_uuid or "").strip()
    if not admin_uuid:
        return TestResult(False, reason="no_admin", name=name, gb=gb, days=days)
    # The customer's link needs the CLIENT proxy path (v12 separates it from the admin one); a panel
    # that hasn't been synced since that feature shipped gets it fetched once here.
    if not panel.client_proxy_path:
        await usercreate.ensure_client_proxy_path(session, panel)
    uid = str(uuidlib.uuid4())
    try:
        uid = await AdminApiClient().create_user(
            panel, name=name, gb=gb, days=days, api_key=admin_uuid, user_uuid=uid,
        )
    except UserLimitError:
        return TestResult(False, reason="limit", name=name, gb=gb, days=days)
    except Exception:  # noqa: BLE001 — the bot reports a clean failure, never a traceback
        log.warning("test config create failed on panel %s", panel.key, exc_info=True)
        return TestResult(False, reason="error", name=name, gb=gb, days=days)
    return TestResult(
        True, sub_link=panel.user_sub_link(uid, name=name), uuid=uid,
        name=name, gb=gb, days=days,
    )
