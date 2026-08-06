"""Prod incident 2026-08-06 — a suspension for an admin that had been deleted from the panel.

A reseller created a sub-admin, tapped «مسدودسازی زیرمجموعه» in the bot, then deleted that
sub-admin in the Hiddify panel before the queue reached the action. Every retry then authenticated
the Flask-Admin bulk page AS the deleted admin, Hiddify answered `302 → login` (not a 4xx), the
scraper found no CSRF token in the empty redirect body, and after five retries the owner was
alerted about a CSRF fault that did not exist. Meanwhile Hiddify had re-parented the sub-admin's
user to the PARENT — an active, paid-up reseller whose customer we were still trying to disable.

Four guarantees, all asserted here:
  1. an action whose target admin is gone is closed as OBSOLETE, not failed, and not retried;
  2. the panel — never our own sync stamp — has the last word on "gone";
  3. a user the panel now attributes to an admin outside the action's subtree is left alone;
  4. an admin deleted mid-run stops the limits phase from burning its retries.
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/enfdeleted.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import EndUserSnapshot, Panel, Reseller  # noqa: E402
from app.models.enums import (  # noqa: E402
    EnforcementActionStatus,
    EnforcementState,
    PanelStatus,
)
from app.services.panel_client.admin_api import (  # noqa: E402
    PanelAuthError,
    UserIdentity,
)

NOW = dt.datetime(2026, 8, 6, 22, 30, tzinfo=dt.timezone.utc)
SEEN_EARLIER = NOW - dt.timedelta(hours=1)      # last sync that still reported the admin


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await coro_fn(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


async def _seed(s, *, still_seen: bool = False, owner_of_user: str = "SUB"):
    """The incident's shape: parent PARENT, sub-admin SUB, one user under SUB."""
    from app.services import settings_service

    await settings_service.set_value(s, "enforcement_enabled", True)
    s.add(Panel(id=1, key="s7", host="h", proxy_path_enc="x", owner_uuid="owner",
                status=PanelStatus.ok, last_synced_at=NOW))
    parent = Reseller(panel_id=1, admin_uuid="PARENT", name="Parent", parent_admin_uuid="owner",
                      panel_max_users=50, panel_max_active_users=50, last_seen_at=NOW,
                      enforcement_state=EnforcementState.active)
    sub = Reseller(panel_id=1, admin_uuid="SUB", name="Sub", parent_admin_uuid="PARENT",
                   panel_max_users=50, panel_max_active_users=50,
                   last_seen_at=NOW if still_seen else SEEN_EARLIER,
                   enforcement_state=EnforcementState.active)
    s.add_all([parent, sub])
    await s.flush()
    s.add(EndUserSnapshot(panel_id=1, user_uuid="u1", name="test",
                          added_by_uuid=owner_of_user, enable=True, last_synced_at=NOW))
    await s.commit()
    return parent, sub


def _patch_panel(monkeypatch, enforcement, *, admin_exists, calls, live_owner="SUB"):
    async def fake_admin_exists(self, panel, admin_uuid, *, api_key=None):
        calls.setdefault("exists", []).append(admin_uuid)
        return admin_exists

    async def fake_identity(self, panel, user_uuid, *, api_key=None):
        return UserIdentity(77, live_owner)

    async def fake_bulk(self, panel, user_ids, enabled, *, api_key=None):
        calls.setdefault("bulk", []).append((tuple(user_ids), api_key))
        if api_key == "SUB" and admin_exists is False:
            raise PanelAuthError("Hiddify rejected the admin key SUB… (302 → /?force=1)")

    async def fake_get_user(self, panel, user_uuid, *, api_key=None):
        return {"enable": False}

    async def fake_get_limits(self, panel, admin_uuid, api_key=None):
        return (50, 50)

    async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
        calls.setdefault("limits", []).append(admin_uuid)
        if admin_uuid == "SUB" and admin_exists is False:
            raise RuntimeError("PATCH admin 404: not found")

    monkeypatch.setattr(enforcement.AdminApiClient, "admin_exists", fake_admin_exists)
    monkeypatch.setattr(enforcement.AdminApiClient, "get_user_identity", fake_identity)
    monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
    monkeypatch.setattr(enforcement.AdminApiClient, "get_user", fake_get_user)
    monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
    monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)


def test_suspension_of_a_deleted_admin_is_closed_as_obsolete(tmp_path, monkeypatch):
    """The incident: five retries and a red owner alert, replaced by one clean, quiet close."""
    from app.services import enforcement

    async def body(s):
        _parent, sub = await _seed(s)
        action = await enforcement.queue_enforcement(s, sub, dry_run=False)
        calls: dict = {}
        _patch_panel(monkeypatch, enforcement, admin_exists=False, calls=calls)

        res = await enforcement.process_enforcement_queue(s, action_limit=5, user_chunk_size=10)

        assert res["obsolete"] == 1 and res["failed"] == 0
        await s.refresh(action)
        assert action.status == EnforcementActionStatus.done
        assert "obsolete" in (action.error or "")
        assert "no longer exists" in (action.error or "")
        # Nothing was written to the panel — not one bulk call, not one limit patch.
        assert "bulk" not in calls and "limits" not in calls
        # And it is not left pending, so it can never be retried into the same wall again.
        assert action.status != EnforcementActionStatus.planned

    _run(body, tmp_path, "obsolete.db")


def test_the_panel_not_our_sync_stamp_decides_that_an_admin_is_gone(tmp_path, monkeypatch):
    """A stale `last_seen_at` (sync lag, a half-finished sync) must never cancel a suspension.

    The local stamp only decides whether asking the panel is worth a round-trip; the 404 is the
    verdict. Here the stamp says "absent" and the panel says "present" — the suspension runs."""
    from app.services import enforcement

    async def body(s):
        _parent, sub = await _seed(s)                 # last_seen_at is an hour behind the panel
        action = await enforcement.queue_enforcement(s, sub, dry_run=False)
        calls: dict = {}
        _patch_panel(monkeypatch, enforcement, admin_exists=True, calls=calls)

        res = await enforcement.process_enforcement_queue(s, action_limit=5, user_chunk_size=10)

        assert res["obsolete"] == 0
        assert calls["exists"] == ["SUB"], "the panel must be asked before cancelling anything"
        assert calls["bulk"] == [((77,), "SUB")], "the suspension must have gone ahead"
        await s.refresh(action)
        assert action.status == EnforcementActionStatus.done
        await s.refresh(sub)
        assert sub.enforcement_state == EnforcementState.enforced

    _run(body, tmp_path, "not_gone.db")


def test_no_panel_round_trip_while_the_admin_is_present_in_the_latest_sync(tmp_path, monkeypatch):
    """The guard is free on the happy path: a reseller our own sync just saw is never queried."""
    from app.services import enforcement

    async def body(s):
        _parent, sub = await _seed(s, still_seen=True)
        await enforcement.queue_enforcement(s, sub, dry_run=False)
        calls: dict = {}
        _patch_panel(monkeypatch, enforcement, admin_exists=True, calls=calls)

        await enforcement.process_enforcement_queue(s, action_limit=5, user_chunk_size=10)

        assert "exists" not in calls, "an extra panel call per action per tick is not free"

    _run(body, tmp_path, "no_roundtrip.db")


def test_a_user_re_parented_outside_the_subtree_is_not_touched(tmp_path, monkeypatch):
    """Deleting a sub-admin moves its users UP to the parent — an active reseller whose customers
    are none of this action's business. The frozen user→owner map would have disabled one."""
    from app.services import enforcement

    async def body(s):
        _parent, sub = await _seed(s, still_seen=True)
        action = await enforcement.queue_enforcement(s, sub, dry_run=False)
        assert (action.snapshot or {}).get("users") == {"u1": "SUB"}   # frozen at queue time
        calls: dict = {}
        # The panel now says the user belongs to PARENT, which is NOT in this action's subtree.
        _patch_panel(monkeypatch, enforcement, admin_exists=True, calls=calls,
                     live_owner="PARENT")

        await enforcement.process_enforcement_queue(s, action_limit=5, user_chunk_size=10)

        assert "bulk" not in calls, "a user owned by somebody else was written to"
        await s.refresh(action)
        assert action.snapshot["progress"]["users_reassigned"] == {"u1": "PARENT"}
        assert action.status == EnforcementActionStatus.done

    _run(body, tmp_path, "reparented.db")


def test_a_user_moved_within_the_subtree_is_still_disabled(tmp_path, monkeypatch):
    """The mirror case: a reseller shuffling users between their OWN admins mid-flight must not
    smuggle them out of a suspension. The write simply follows the owner the panel reports."""
    from app.services import enforcement

    async def body(s):
        parent, _sub = await _seed(s, still_seen=True, owner_of_user="PARENT")
        await enforcement.queue_enforcement(s, parent, dry_run=False)
        calls: dict = {}
        _patch_panel(monkeypatch, enforcement, admin_exists=True, calls=calls, live_owner="SUB")

        await enforcement.process_enforcement_queue(s, action_limit=5, user_chunk_size=10)

        assert calls["bulk"] == [((77,), "SUB")], (
            "the user moved to a sub-admin inside the bundle and must still be disabled, "
            "under the key that now owns it"
        )

    _run(body, tmp_path, "moved_inside.db")


def test_a_sub_admin_deleted_mid_run_does_not_burn_the_limits_retries(tmp_path, monkeypatch):
    """The parent is alive (so the action is not obsolete) but one sub-admin in its subtree is
    gone. Zeroing limits on a row that does not exist can only 404 — record it as missing rather
    than retrying five times and failing the whole suspension."""
    from app.services import enforcement

    async def body(s):
        parent, _sub = await _seed(s, still_seen=True, owner_of_user="PARENT")
        action = await enforcement.queue_enforcement(s, parent, dry_run=False)
        calls: dict = {}

        async def fake_admin_exists(self, panel, admin_uuid, *, api_key=None):
            return admin_uuid != "SUB"           # only the sub-admin is gone

        _patch_panel(monkeypatch, enforcement, admin_exists=True, calls=calls,
                     live_owner="PARENT")
        monkeypatch.setattr(enforcement.AdminApiClient, "admin_exists", fake_admin_exists)

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            calls.setdefault("limits", []).append(admin_uuid)
            if admin_uuid == "SUB":
                raise RuntimeError("PATCH admin 404: not found")

        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        res = await enforcement.process_enforcement_queue(s, action_limit=5, user_chunk_size=10)

        assert res["failed"] == 0
        await s.refresh(action)
        assert action.status == EnforcementActionStatus.done
        assert "SUB" in action.snapshot["progress"]["admins_missing"]
        assert action.snapshot["progress"]["admin_attempts"].get("SUB") is None

    _run(body, tmp_path, "sub_gone.db")
