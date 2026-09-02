"""Reseller-driven end-user creation (from the bot): bounded options, a capacity guard, and the
create loop. Each create goes through `AdminApiClient.create_user` (which authenticates AS the
reseller so `added_by_uuid` is theirs, and runs `quick_apply_users` so the user works immediately).
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EndUserSnapshot, Panel, Reseller
from app.models.enums import EnforcementState
from app.services import settings_service
from app.services.panel_client.admin_api import AdminApiClient, UserLimitError
from app.services.presence import snapshot_present_filter

log = logging.getLogger("usercreate")

_GB_KEY = "user_create_gb_options"
_DAY_KEY = "user_create_day_options"
_COUNT_KEY = "user_create_bulk_counts"


@dataclass
class CreateOptions:
    enabled: bool
    gb: list[int]
    days: list[int]
    counts: list[int]


async def load_options(session: AsyncSession) -> CreateOptions:
    cfg = await settings_service.get_many(
        session, ["user_create_enabled", _GB_KEY, _DAY_KEY, _COUNT_KEY]
    )
    def _ints(v: object) -> list[int]:
        return [int(x) for x in v] if isinstance(v, list) else []
    return CreateOptions(
        enabled=bool(cfg.get("user_create_enabled")),
        gb=_ints(cfg.get(_GB_KEY)),
        days=_ints(cfg.get(_DAY_KEY)),
        counts=_ints(cfg.get(_COUNT_KEY)),
    )


def qr_png(data: str) -> bytes:
    """PNG bytes of a QR code for `data` (e.g. a subscription link)."""
    import qrcode

    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf)  # qrcode's default image writes PNG (matches loginsec.totp_qr_data_uri)
    return buf.getvalue()


async def current_user_count(session: AsyncSession, reseller: Reseller) -> int:
    """How many end-users this reseller currently OCCUPIES on its panel (case-insensitive added_by).

    Counts only users present in the panel's latest sync. Snapshots of users deleted from Hiddify
    are retained for billing (see `services/presence`), but they occupy no slot on the panel, so
    counting them here would charge the reseller capacity for services that no longer exist and
    block creates that Hiddify itself would happily accept.
    """
    return (
        await session.execute(
            select(func.count(EndUserSnapshot.user_uuid))
            .join(Panel, Panel.id == EndUserSnapshot.panel_id)
            .where(
                EndUserSnapshot.panel_id == reseller.panel_id,
                func.lower(EndUserSnapshot.added_by_uuid) == (reseller.admin_uuid or "").lower(),
                snapshot_present_filter(),
            )
        )
    ).scalar_one()


@dataclass
class CreatedUser:
    name: str
    uuid: str
    sub_link: str


@dataclass
class CreateResult:
    created: list[CreatedUser] = field(default_factory=list)
    suspended: bool = False          # the reseller (or its branch) is cut off — nothing was asked of the panel
    blocked_reason: str | None = None  # "suspended" | "frozen" when `suspended` is set
    capacity_blocked: bool = False   # pre-check: creating `count` would exceed max_users
    limit_hit: bool = False          # the panel itself rejected mid-run (server max_users)
    error: str | None = None
    max_users: int | None = None
    remaining: int | None = None     # capacity left at pre-check (when max_users is set)


async def creation_blocked(session: AsyncSession, reseller: Reseller) -> str | None:
    """Why this reseller may not create users right now — `"suspended"` when it (or an ancestor) is
    under a live suspension, `"frozen"` when its own limits were zeroed by a freeze — else None.

    THE gate for every create path (bot «ساخت کاربر», storefront provisioning). It exists because
    nothing downstream refuses: the create authenticates with the reseller's API key, which Hiddify
    keeps honouring after the enforcement password lock (that lock only closes the UUID-link web
    login), and Hiddify's REST create never consults `max_users` — `can_have_more_users()` is
    called from the Flask-Admin UI only. So a suspended debtor whose users were all disabled and
    whose limits read 0/0 could still mint a fresh user a day through the bot (11 resellers / 46
    users found in production on 2026-09-02); the daily re-assert killed each one, but never the
    door. Imported lazily: `enforcement` pulls in the panel client and would close an import cycle
    through this module."""
    from app.services import enforcement

    if reseller.enforcement_state == EnforcementState.frozen:
        return "frozen"
    if await enforcement.suspended_branch_root(session, reseller) is not None:
        return "suspended"
    return None


async def create_for_reseller(
    session: AsyncSession, reseller: Reseller, *, count: int, gb: int, days: int, base_name: str,
    user_uuid: str | None = None,
) -> CreateResult:
    """Create `count` users under `reseller` (names: the base for a single, `<base>1..<base>N` for
    bulk). Capacity is pre-checked against the reseller's `panel_max_users`; the panel also enforces
    it server-side (a rejection sets `limit_hit`). Stops at the first failure and returns what was
    created so the caller can report partial success.

    `user_uuid` (single-create only) pins the new user's uuid — the storefront pre-generates it and
    records it on the order BEFORE provisioning, so a crash mid-create is recoverable (the reaper can
    look the uuid up on the panel)."""
    panel = await session.get(Panel, reseller.panel_id)
    res = CreateResult(max_users=reseller.panel_max_users)
    if panel is None:
        res.error = "panel not found"
        return res
    # Refuse BEFORE any network I/O: a cut-off reseller gets no proxy-path fetch and no create.
    why = await creation_blocked(session, reseller)
    if why is not None:
        res.suspended = True
        res.blocked_reason = why
        return res
    # The customer sub-link needs the CLIENT proxy path (v12 separates it from the admin path). It's
    # captured during sync; if a panel hasn't been re-synced since the feature shipped, fetch it once
    # now so the very first create still produces the correct link.
    if not panel.client_proxy_path:
        await ensure_client_proxy_path(session, panel)
    # `is not None`, not truthiness: a synced limit of 0 is a REAL cap (a suspension or freeze
    # zeroes it, and Hiddify's own UI treats 0 as "no more users"). The old truthy test skipped the
    # guard exactly when the limit had been zeroed, reading "blocked" as "unlimited".
    if reseller.panel_max_users is not None:
        current = await current_user_count(session, reseller)
        res.remaining = max(0, reseller.panel_max_users - current)
        if current + count > reseller.panel_max_users:
            res.capacity_blocked = True
            return res
    client = AdminApiClient()
    for i in range(count):
        name = base_name if count == 1 else f"{base_name}{i + 1}"
        try:
            uid = await client.create_user(
                panel, name=name, gb=gb, days=days, api_key=reseller.admin_uuid,
                user_uuid=user_uuid if count == 1 else None,
            )
        except UserLimitError:
            res.limit_hit = True
            break
        except Exception as exc:  # noqa: BLE001 — surface a clean partial result to the bot
            log.warning("create_user failed for %s on panel %s", reseller.name, panel.key, exc_info=True)
            res.error = str(exc)
            break
        res.created.append(
            CreatedUser(name=name, uuid=uid, sub_link=panel.user_sub_link(uid, name=name))
        )
    return res


async def ensure_client_proxy_path(session: AsyncSession, panel: Panel) -> None:
    """Best-effort one-off fetch of the panel's client proxy path from its backup (when sync hasn't
    captured it yet). Never raises — a failure just leaves `user_sub_link` on the admin-path fallback."""
    from app.services.panel_client import BackupJsonClient

    try:
        data = await BackupJsonClient().fetch_backup(panel)
        if data.client_proxy_path:
            panel.client_proxy_path = data.client_proxy_path
            await session.commit()
    except Exception:  # noqa: BLE001
        log.warning("could not fetch client proxy path for panel %s", panel.key, exc_info=True)
