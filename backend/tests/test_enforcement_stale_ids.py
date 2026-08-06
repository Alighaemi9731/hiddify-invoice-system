"""Prod incident 2026-07-18 — a suspension disabled 305 OTHER resellers' users.

Root cause: `EndUserSnapshot.panel_user_id` cached Hiddify's numeric user id durably and every
later enforcement reused it WITHOUT re-resolving. Hiddify renumbers its user table on a panel
restore/re-import, so those cached ids came to belong to completely different users. The bulk
action was handed stale rowids, disabled whoever owns them now (305 innocent users across ~20
resellers), left the actual target's users enabled — and, because the panel still answered 200,
recorded the action as a success.

Two guards, both asserted here:
  1. ids are ALWAYS resolved fresh from the panel; the durable cache is never a source of truth.
  2. a write that reports success but did not land on our users FAILS the action instead of
     silently finalizing (the miss must be loud).
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/enfstale.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import (  # noqa: E402
    EndUserSnapshot,
    EnforcementAction,
    Invoice,
    Panel,
    Reseller,
)
from app.models.enums import (  # noqa: E402
    EnforcementActionStatus,
    EnforcementActionType,
    EnforcementState,
    InvoiceStatus,
)
from tests.panel_fakes import as_identity  # noqa: E402


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await coro_fn(s, Session)
        finally:
            await engine.dispose()
    asyncio.run(go())


def _invoice(reseller_id):
    return Invoice(
        reseller_id=reseller_id, panel_id=1, period_start=dt.date(2026, 3, 1),
        period_end=dt.date(2026, 3, 31), period_label="2026-03", usage_gb=10,
        amount_toman=10000, amount_usdt=1, status=InvoiceStatus.sent,
        sent_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5),
    )


async def _seed(s, *, stale_ids: bool):
    """Two users of reseller A. When `stale_ids`, their cached panel_user_id is the value the
    panel handed out BEFORE it was renumbered — it now belongs to another reseller's users."""
    from app.services import settings_service

    await settings_service.set_value(s, "enforcement_enabled", True)
    s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
    r = Reseller(panel_id=1, admin_uuid="A", name="R", panel_max_users=10,
                 panel_max_active_users=10, enforcement_state=EnforcementState.active)
    s.add(r)
    await s.flush()
    for i in range(2):
        s.add(EndUserSnapshot(
            panel_id=1, user_uuid=f"u{i}", name=f"u{i}", added_by_uuid="A", enable=True,
            panel_user_id=(900 + i) if stale_ids else None,   # 900/901 = the STALE ids
        ))
    inv = _invoice(r.id)
    s.add(inv)
    await s.commit()
    return r, inv


# The panel AFTER renumbering: our users now live at 10/11. 900/901 belong to somebody else.
FRESH = {"u0": 10, "u1": 11}


def test_stale_cached_panel_user_id_is_never_reused(tmp_path, monkeypatch):
    """The incident itself: with a poisoned durable cache the old code sent 900/901 (another
    reseller's rows) and never asked the panel. Now every id is resolved fresh, so ONLY the
    real ids 10/11 reach the bulk action."""
    from app.services import enforcement

    async def body(s, _Session):
        r, inv = await _seed(s, stale_ids=True)
        action = await enforcement.queue_enforcement(s, r, invoice_id=inv.id, dry_run=False)
        assert action.status == EnforcementActionStatus.planned

        looked_up: list[str] = []
        sent_ids: list[int] = []

        async def fake_user_id(self, panel, user_uuid, *, api_key=None):
            looked_up.append(user_uuid)
            return FRESH.get(user_uuid)

        async def fake_bulk(self, panel, user_ids, enabled, *, api_key=None):
            sent_ids.extend(user_ids)

        async def fake_get_user(self, panel, user_uuid, *, api_key=None):
            return {"enable": False}          # the write landed (verification passes)

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            return (10, 10)

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            return None

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_identity", as_identity(fake_user_id))
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_user", fake_get_user)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        await enforcement.process_enforcement_queue(s, action_limit=5, user_chunk_size=10)

        # Every target was re-resolved against the panel (the cache is not a source of truth).
        assert sorted(looked_up) == ["u0", "u1"], f"stale cache was reused: {looked_up}"
        # ONLY the fresh ids were written — the stale 900/901 never reached the panel.
        assert sorted(sent_ids) == [10, 11], f"wrong rowids sent: {sent_ids}"
        assert 900 not in sent_ids and 901 not in sent_ids

    _run(body, tmp_path, "stale_ids.db")


def test_bulk_write_that_missed_its_targets_fails_loudly(tmp_path, monkeypatch):
    """The silent-failure half: Hiddify answers 200 while changing nothing we intended. Without
    a read-back the action finalized as `done` (that is how the incident went unnoticed). Now a
    majority-wrong sample fails the action so it is visible and retryable."""
    from app.services import enforcement

    async def body(s, _Session):
        r, inv = await _seed(s, stale_ids=False)
        action = await enforcement.queue_enforcement(s, r, invoice_id=inv.id, dry_run=False)

        async def fake_user_id(self, panel, user_uuid, *, api_key=None):
            return FRESH.get(user_uuid)

        async def fake_bulk(self, panel, user_ids, enabled, *, api_key=None):
            return None                        # "succeeds" but changes nothing (the real bug)

        async def fake_get_user(self, panel, user_uuid, *, api_key=None):
            return {"enable": True}            # still enabled → the write missed

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            return (10, 10)

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            return None

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_identity", as_identity(fake_user_id))
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_user", fake_get_user)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        await enforcement.process_enforcement_queue(s, action_limit=5, user_chunk_size=10)

        await s.refresh(action)
        assert action.status == EnforcementActionStatus.failed, (
            f"a write that missed every target was reported as {action.status}")
        assert "verification failed" in (action.error or "")
        # And the reseller was NOT left looking successfully enforced.
        await s.refresh(r)
        assert r.enforcement_state != EnforcementState.enforced

    _run(body, tmp_path, "missed_write.db")


async def _seed_two_admins(s):
    """A parent reseller A with a sub-reseller B, one user each — so one suspension spans two
    owning admins and must send a SEPARATE, correctly-keyed batch per admin."""
    from app.services import settings_service

    await settings_service.set_value(s, "enforcement_enabled", True)
    s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
    parent = Reseller(panel_id=1, admin_uuid="A", name="Parent", parent_admin_uuid="owner",
                      panel_max_users=10, panel_max_active_users=10,
                      enforcement_state=EnforcementState.active)
    sub = Reseller(panel_id=1, admin_uuid="B", name="Sub", parent_admin_uuid="A",
                   panel_max_users=5, panel_max_active_users=5,
                   enforcement_state=EnforcementState.active)
    s.add_all([parent, sub])
    await s.flush()
    s.add_all([
        EndUserSnapshot(panel_id=1, user_uuid="ua", name="ua", added_by_uuid="A", enable=True),
        EndUserSnapshot(panel_id=1, user_uuid="ub", name="ub", added_by_uuid="B", enable=True),
    ])
    inv = _invoice(parent.id)
    s.add(inv)
    await s.commit()
    return parent, inv


def test_each_bulk_batch_runs_under_its_owning_admins_key(tmp_path, monkeypatch):
    """The amplifier fix: bulk writes used to run as the panel SUPER-ADMIN, so Hiddify had no
    reason to refuse a rowid belonging to somebody else — that is what turned a wrong-id bug into
    305 disabled users across ~20 other resellers. Each batch must now be keyed to the admin that
    owns those users, making the panel itself refuse anything out of scope."""
    from app.services import enforcement

    async def body(s, _Session):
        parent, inv = await _seed_two_admins(s)
        await enforcement.queue_enforcement(s, parent, invoice_id=inv.id, dry_run=False)

        calls: list[tuple] = []

        async def fake_user_id(self, panel, user_uuid, *, api_key=None):
            return {"ua": 10, "ub": 20}.get(user_uuid)

        async def fake_bulk(self, panel, user_ids, enabled, *, api_key=None):
            calls.append((tuple(sorted(user_ids)), api_key))

        async def fake_get_user(self, panel, user_uuid, *, api_key=None):
            return {"enable": False}

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            return (10, 10)

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            return None

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_identity", as_identity(fake_user_id))
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_user", fake_get_user)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        await enforcement.process_enforcement_queue(s, action_limit=5, user_chunk_size=50)

        # Two admins → two separately-keyed batches, never one super-admin batch of both users.
        assert sorted(calls) == [((10,), "A"), ((20,), "B")], f"got: {calls}"
        assert all(key for _ids, key in calls), "a batch ran without an owning-admin key"

    _run(body, tmp_path, "scoped_bulk.db")


def test_suspension_is_reasserted_when_a_debtor_re_enables_their_users(tmp_path, monkeypatch):
    """Observed live on five resellers, one within three hours of being suspended.

    Zeroing a reseller's limits stops them CREATING users — it does not remove their Hiddify admin
    login, so a suspended debtor can simply re-enable their existing users. Nothing looked again:
    dunning only triggers for an `active` reseller and `queue_enforcement` returns the prior `done`
    action for an `enforced` one. They were back in business with the invoice unpaid, silently."""
    from app.services import enforcement

    async def body(s, _Session):
        r, inv = await _seed(s, stale_ids=False)
        r.enforcement_state = EnforcementState.enforced          # already suspended
        r.max_users_snapshot, r.max_active_users_snapshot = 600, 600   # the restore source
        s.add(EnforcementAction(
            reseller_id=r.id, action=EnforcementActionType.disable_users,
            status=EnforcementActionStatus.done, dry_run=False, snapshot={}))
        await s.commit()

        # The debtor turns their users back on; the next sync records them as enabled.
        for row in (await s.execute(select(EndUserSnapshot))).scalars().all():
            row.enable = True
        await s.commit()

        out = await enforcement.reassert_enforced(s)
        assert out["requeued"] == 1, f"drift not detected: {out}"

        queued = (await s.execute(
            select(EnforcementAction).where(
                EnforcementAction.status == EnforcementActionStatus.planned))).scalars().first()
        assert queued is not None
        assert set(queued.snapshot["users"]) == {"u0", "u1"}
        # USERS-ONLY: re-capturing limits here would overwrite the pre-suspension values with the
        # zeros now on the panel and destroy the ability to ever restore them.
        assert queued.snapshot["admins"] == [] and queued.snapshot["limits"] == {}
        await s.refresh(r)
        assert (r.max_users_snapshot, r.max_active_users_snapshot) == (600, 600)

        # Idempotent: a second sweep must not pile up duplicate actions.
        assert (await enforcement.reassert_enforced(s))["requeued"] == 0

    _run(body, tmp_path, "reassert.db")


def test_reassert_leaves_a_settled_reseller_alone(tmp_path, monkeypatch):
    """No debt → not our business; re-suspending a reseller who has paid would be worse than the
    bug. (The restore path owns that transition.)"""
    from app.models.enums import InvoiceStatus
    from app.services import enforcement

    async def body(s, _Session):
        r, inv = await _seed(s, stale_ids=False)
        r.enforcement_state = EnforcementState.enforced
        inv.status = InvoiceStatus.paid                    # debt settled
        for row in (await s.execute(select(EndUserSnapshot))).scalars().all():
            row.enable = True
        await s.commit()
        assert (await enforcement.reassert_enforced(s))["requeued"] == 0

    _run(body, tmp_path, "reassert_paid.db")


async def _seed_reassert_presence(s, *, fresh: int, stale: int):
    """An ENFORCED reseller with an unpaid invoice, whose users are enabled again.

    `fresh` rows were seen in the panel's latest sync; `stale` rows are retained snapshots of users
    since deleted from Hiddify (kept for billing, but gone from the panel).
    """
    from app.services import settings_service

    await settings_service.set_value(s, "enforcement_enabled", True)
    synced = dt.datetime(2026, 3, 20, 9, 0, tzinfo=dt.timezone.utc)
    s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner",
                last_synced_at=synced))
    r = Reseller(panel_id=1, admin_uuid="A", name="R", panel_max_users=10,
                 panel_max_active_users=10, enforcement_state=EnforcementState.enforced,
                 max_users_snapshot=600, max_active_users_snapshot=600)
    s.add(r)
    await s.flush()
    for i in range(fresh):
        # Sync stamps the SAME instant on the panel and on every user it saw.
        s.add(EndUserSnapshot(panel_id=1, user_uuid=f"fresh{i}", name=f"fresh{i}",
                              added_by_uuid="A", enable=True, last_synced_at=synced))
    for i in range(stale):
        s.add(EndUserSnapshot(panel_id=1, user_uuid=f"stale{i}", name=f"stale{i}",
                              added_by_uuid="A", enable=True,
                              last_synced_at=synced - dt.timedelta(days=1)))
    s.add(_invoice(r.id))
    await s.commit()
    return r


def _queued_disable_actions(s):
    return (
        select(EnforcementAction)
        .where(EnforcementAction.action == EnforcementActionType.disable_users)
        .order_by(EnforcementAction.id)
    )


def test_reassert_ignores_users_deleted_from_the_panel(tmp_path):
    """A retained snapshot must not look like a debtor re-enabling their service.

    Deleted users keep `enable=True` forever (nothing clears it — the column mirrors panel truth
    and is read by billing), and the queue worker can only resolve them as "missing on the panel"
    without changing the row. The duplicate guard only covers planned/partial actions, so once the
    no-op action COMPLETED the next daily sweep queued an identical one — every day, for the whole
    ~2-month retention window, each pass re-running the limits phase and logging a false alarm.
    """
    async def body(s, _Session):
        from app.services import enforcement

        await _seed_reassert_presence(s, fresh=0, stale=2)
        out = await enforcement.reassert_enforced(s)
        assert out["requeued"] == 0, "queued a disable for users that no longer exist on the panel"
        assert (await s.execute(_queued_disable_actions(s))).scalars().all() == []

    _run(body, tmp_path, "reassert_stale.db")


def test_reassert_still_catches_a_user_who_really_is_back_online(tmp_path):
    """The guard must not blunt the real detection it exists for."""
    async def body(s, _Session):
        from app.services import enforcement

        await _seed_reassert_presence(s, fresh=2, stale=0)
        out = await enforcement.reassert_enforced(s)
        assert out["requeued"] == 1
        act = (await s.execute(_queued_disable_actions(s))).scalars().all()[0]
        assert set(act.snapshot["users"]) == {"fresh0", "fresh1"}
        assert act.snapshot["reassert"] is True

    _run(body, tmp_path, "reassert_fresh.db")


def test_reassert_mixes_only_the_present_users_into_the_action(tmp_path):
    async def body(s, _Session):
        from app.services import enforcement

        await _seed_reassert_presence(s, fresh=2, stale=3)
        out = await enforcement.reassert_enforced(s)
        assert out["requeued"] == 1
        act = (await s.execute(_queued_disable_actions(s))).scalars().all()[0]
        assert set(act.snapshot["users"]) == {"fresh0", "fresh1"}
        assert act.affected_count == 2

    _run(body, tmp_path, "reassert_mixed.db")


def test_reassert_does_not_repeat_after_the_action_completed(tmp_path):
    """Once the sweep's action has run to completion, a later sweep must not re-queue the same
    work for rows that are only still `enable=True` because they were deleted."""
    async def body(s, _Session):
        from app.services import enforcement

        await _seed_reassert_presence(s, fresh=1, stale=2)
        assert (await enforcement.reassert_enforced(s))["requeued"] == 1
        act = (await s.execute(_queued_disable_actions(s))).scalars().all()[0]
        # Simulate the worker finishing: the present user really got disabled; the deleted rows
        # were resolved as missing and (correctly) left untouched.
        row = (await s.execute(
            select(EndUserSnapshot).where(EndUserSnapshot.user_uuid == "fresh0")
        )).scalar_one()
        row.enable = False
        act.status = EnforcementActionStatus.done
        await s.commit()

        assert (await enforcement.reassert_enforced(s))["requeued"] == 0
        assert len((await s.execute(_queued_disable_actions(s))).scalars().all()) == 1

    _run(body, tmp_path, "reassert_norepeat.db")


def test_reassert_on_a_never_synced_panel_falls_open(tmp_path):
    """No successful sync → we cannot claim a user was deleted, so keep detecting them. Losing a
    real re-enable is worse than one redundant action."""
    async def body(s, _Session):
        from app.services import enforcement, settings_service

        await settings_service.set_value(s, "enforcement_enabled", True)
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R", panel_max_users=10,
                     panel_max_active_users=10,
                     enforcement_state=EnforcementState.enforced)
        s.add(r)
        await s.flush()
        s.add(EndUserSnapshot(panel_id=1, user_uuid="u0", name="u0", added_by_uuid="A",
                              enable=True))
        s.add(_invoice(r.id))
        await s.commit()

        assert (await enforcement.reassert_enforced(s))["requeued"] == 1

    _run(body, tmp_path, "reassert_nosync.db")


def test_reassert_action_touches_no_admin_limits(tmp_path, monkeypatch):
    """A users-only re-assert must NOT re-run the admin-limits phase.

    `reassert_enforced` sets `admins: []` to mean "touch no limits", but the worker read it as
    `snapshot.get("admins") or [all descendants]` — and `[]` is falsy, so the instruction was
    silently inverted into "every descendant". Each daily sweep then re-captured
    max_users_snapshot from the already-zeroed panel: the same shape as the M38 bug that
    permanently destroyed real max_users values.
    """
    from app.services import enforcement

    async def body(s, _Session):
        await _seed_reassert_presence(s, fresh=1, stale=0)
        assert (await enforcement.reassert_enforced(s))["requeued"] == 1

        limit_writes: list[tuple] = []

        async def fake_user_id(self, panel, user_uuid, *, api_key=None):
            return 42

        async def fake_bulk(self, panel, user_ids, enabled, *, api_key=None):
            return None

        async def fake_get_user(self, panel, user_uuid, *, api_key=None):
            return {"enable": False}

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            return (0, 0)          # already suspended → the panel reads zeros

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            limit_writes.append((admin_uuid, mu, mau))

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_identity", as_identity(fake_user_id))
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_user", fake_get_user)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        await enforcement.process_enforcement_queue(s, action_limit=5, user_chunk_size=10)

        assert limit_writes == [], f"users-only re-assert wrote admin limits: {limit_writes}"
        # …and the real pre-suspension limits survive untouched.
        r = (await s.execute(select(Reseller).where(Reseller.admin_uuid == "A"))).scalar_one()
        await s.refresh(r)
        assert r.max_users_snapshot == 600 and r.max_active_users_snapshot == 600

    _run(body, tmp_path, "reassert_limits.db")


def test_reassert_action_actually_disables_the_users(tmp_path, monkeypatch):
    """The re-assertion path must reach the panel — it was queueing work nobody ever executed.

    `reassert_enforced` only ever selects resellers already in `enforced` state, but the worker's
    M38 idempotency guard finalized any suspension for an already-enforced reseller as `done`
    without touching the panel. So every queued re-assert was discarded unexecuted, the debtor's
    re-enabled users stayed online, and the sweep re-queued the same no-op the next day. The guard
    is exempted for re-asserts only, which is safe because they are users-only (asserted by
    `test_reassert_action_touches_no_admin_limits`).
    """
    from app.services import enforcement

    async def body(s, _Session):
        await _seed_reassert_presence(s, fresh=2, stale=0)
        assert (await enforcement.reassert_enforced(s))["requeued"] == 1

        disabled: list[int] = []

        async def fake_user_id(self, panel, user_uuid, *, api_key=None):
            return {"fresh0": 10, "fresh1": 11}[user_uuid]

        async def fake_bulk(self, panel, user_ids, enabled, *, api_key=None):
            assert enabled is False
            disabled.extend(user_ids)

        async def fake_get_user(self, panel, user_uuid, *, api_key=None):
            return {"enable": False}          # the write landed

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_identity", as_identity(fake_user_id))
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_user", fake_get_user)

        res = await enforcement.process_enforcement_queue(s, action_limit=5, user_chunk_size=10)

        assert sorted(disabled) == [10, 11], "re-assert never reached the panel"
        assert res["patched_users"] == 2
        rows = (await s.execute(select(EndUserSnapshot))).scalars().all()
        assert all(r.enable is False for r in rows)

    _run(body, tmp_path, "reassert_executes.db")


def test_a_normal_suspension_of_an_enforced_reseller_is_still_idempotent(tmp_path, monkeypatch):
    """The M38 guard must survive for ordinary suspensions — only re-asserts are exempt."""
    from app.services import enforcement

    async def body(s, _Session):
        r = await _seed_reassert_presence(s, fresh=1, stale=0)
        act = EnforcementAction(
            reseller_id=r.id, action=EnforcementActionType.disable_users, dry_run=False,
            affected_count=1, status=EnforcementActionStatus.planned,
            snapshot={"limits": {}, "admins": ["A"], "users": {"fresh0": "A"}},
        )
        s.add(act)
        await s.commit()

        touched: list[str] = []

        async def boom(self, *a, **k):
            touched.append("panel")
            raise AssertionError("an already-enforced reseller must not be re-suspended")

        for name in ("get_user_identity", "bulk_set_users_enabled", "get_admin_limits",
                     "set_admin_limits", "admin_exists"):
            monkeypatch.setattr(enforcement.AdminApiClient, name, boom)

        await enforcement.process_enforcement_queue(s, action_limit=5, user_chunk_size=10)
        assert touched == []
        await s.refresh(act)
        assert act.status == EnforcementActionStatus.done

    _run(body, tmp_path, "reassert_guard_kept.db")
