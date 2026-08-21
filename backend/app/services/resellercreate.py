"""The owner's one-tap «➕ نمایندهٔ جدید» — create a reseller (a Hiddify admin) from the bot.

Creating the admin in the panel's own UI meant the reseller could not register in the bot until
`periodic_sync` next crossed that panel: `intake._registration_candidate` matches a pasted link
against EXISTING `Reseller` rows and is fail-closed, so until then every link answers "not found".
Here we create the admin ourselves, so the uuid, the panel and the parent are known facts — and the
local row is written in the same breath. The reseller can register immediately, with no sync.

Three things are deliberate:

* The admin is created AS the panel's super-admin (`api_key = panel.owner_uuid`) AND parented on it
  explicitly. Hiddify parents a new admin on the acting account when no parent is sent, and the
  header fallback is `panel.admin_api_key` — possibly a different admin entirely. That would make
  the new reseller a SUB-reseller: `_is_top_level_reseller` would refuse its registration and
  `invoice_engine` would bill it inside another reseller's bundle.
* NO password is set. Hiddify's `auth_before_request` rejects UUID-link login whenever
  `account.password != ""`, so a default password would hand the reseller a dead panel link. A
  fresh admin's password is empty; that is exactly what makes the link we hand back work.
* The local row mirrors what `sync._upsert_resellers` writes (lowercased uuid, `mode="agent"`,
  `last_seen_at` stamped), so the next sync updates it in place instead of creating a twin.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid as uuidlib
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Panel, Reseller
from app.services.panel_client.admin_api import AdminApiClient, AdminExistsError
from app.services.testconfig import (
    panel_choices,  # noqa: F401 — same query, re-exported
)

log = logging.getLogger("resellercreate")

# The panel's own defaults, and the median of all 756 resellers in production. Raising a specific
# reseller later is what the existing «افزایش ظرفیت» flow (`admin_capacity.bump_limits`) is for.
DEFAULT_MAX_USERS = 100
DEFAULT_MAX_ACTIVE_USERS = 100

NAME_MAX_LEN = 64


@dataclass
class NewResellerResult:
    ok: bool
    name: str = ""
    admin_uuid: str | None = None
    link: str | None = None
    reseller_id: int | None = None
    saved: bool = True  # False = created on the panel but the local row didn't commit
    reason: str | None = None  # "no_admin" | "exists" | "error"


def clean_name(raw: str | None) -> str:
    """Normalize a typed reseller name; returns "" when it isn't usable as one."""
    name = " ".join((raw or "").split())
    if not name or name.startswith("/"):  # a slash command is a mis-typed name, never a name
        return ""
    return name[:NAME_MAX_LEN]


async def create(session: AsyncSession, panel: Panel, *, name: str) -> NewResellerResult:
    """Create ONE reseller on `panel` and register it locally. Returns its admin link."""
    owner_uuid = (panel.owner_uuid or "").strip()
    if not owner_uuid:
        return NewResellerResult(False, name=name, reason="no_admin")
    uid = str(uuidlib.uuid4())
    try:
        uid = await AdminApiClient().create_admin(
            panel,
            name=name,
            api_key=owner_uuid,
            parent_admin_uuid=owner_uuid,
            admin_uuid=uid,
            max_users=DEFAULT_MAX_USERS,
            max_active_users=DEFAULT_MAX_ACTIVE_USERS,
        )
    except AdminExistsError:
        return NewResellerResult(False, name=name, reason="exists")
    except Exception:  # noqa: BLE001 — the bot reports a clean failure, never a traceback
        log.warning("reseller create failed on panel %s", panel.key, exc_info=True)
        return NewResellerResult(False, name=name, reason="error")

    link = panel.admin_link(uid, tag=name)
    result = NewResellerResult(True, name=name, admin_uuid=uid, link=link)
    try:
        result.reseller_id = await _register_locally(session, panel, uid, name, owner_uuid)
    except Exception:  # noqa: BLE001
        # The admin EXISTS on the panel now; the next sync will pick it up. Losing the link the
        # owner is waiting for would be the worse failure, so report it either way.
        await session.rollback()
        log.warning("reseller %s created on %s but not saved locally", uid, panel.key,
                    exc_info=True)
        result.saved = False
    return result


async def _register_locally(
    session: AsyncSession, panel: Panel, admin_uuid: str, name: str, owner_uuid: str
) -> int:
    """Insert the `Reseller` row the sync would have inserted, and return its id."""
    existing = (
        await session.execute(
            select(Reseller).where(
                Reseller.panel_id == panel.id,
                func.lower(Reseller.admin_uuid) == admin_uuid.lower(),
            )
        )
    ).scalars().first()
    row = existing or Reseller(panel_id=panel.id, admin_uuid=admin_uuid.lower())
    row.name = name
    row.parent_admin_uuid = owner_uuid.lower()
    row.mode = "agent"
    row.is_owner = False
    row.can_add_admin = False
    row.panel_max_users = DEFAULT_MAX_USERS
    row.panel_max_active_users = DEFAULT_MAX_ACTIVE_USERS
    row.last_seen_at = dt.datetime.now(dt.timezone.utc)
    if existing is None:
        session.add(row)
    await session.commit()
    return row.id
