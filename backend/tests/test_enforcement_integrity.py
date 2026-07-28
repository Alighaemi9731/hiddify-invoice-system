"""Enforcement integrity: restore-source selection, snapshot preservation, capture poisoning,
cascade state, and the storefront suspension guard.

Every case here is a production-observed failure mode (NOrouzi, 2026-07-24):

* A re-assert (users-only, `limits: {}`) became the restore SOURCE, so restore re-enabled every user,
  restored NO limits, and then NULLed `max_users_snapshot` — leaving the reseller online but unable
  to create a single user, with the real limits gone.
* The next suspend then captured the panel's zero as the "real" limit, so every later restore handed
  back 0 — self-perpetuating.
* Descendants were never marked suspended in the DB, so a branch of a suspended tree looked active.
* The storefront paths wrote `enable=True` to the panel with no enforcement check at all.
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/enfint.db")
os.environ.setdefault("SECRET_KEY", "k")

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
from app.services import enforcement  # noqa: E402


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


def _owed_invoice(reseller_id: int) -> Invoice:
    return Invoice(
        reseller_id=reseller_id, panel_id=1, period_start=dt.date(2026, 3, 1),
        period_end=dt.date(2026, 3, 31), period_label="2026-03", usage_gb=10,
        amount_toman=10000, amount_usdt=1, status=InvoiceStatus.sent,
        sent_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40),
    )


def _progress(**over) -> dict:
    base = {
        "phase": "done", "users_done": [], "users_missing": [], "users_failed": {},
        "user_attempts": {}, "admins_done": [], "admins_missing": [], "admins_failed": {},
        "admin_attempts": {}, "captured_limits": {},
    }
    base.update(over)
    return base


# ── Bug 1a: a users-only re-assert must never become the restore source ───────

def test_restore_source_skips_reassert_and_absorbs_its_users(tmp_path):
    """The newest action is a re-assert carrying no limits. The restore must take its limits from
    the older REAL suspend, while still re-enabling the users the re-assert disabled."""

    async def body(s, Session):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R", panel_max_users=1000,
                     panel_max_active_users=1000, max_users_snapshot=1000,
                     max_active_users_snapshot=1000,
                     enforcement_state=EnforcementState.enforced)
        s.add(r)
        await s.flush()

        real_limits = {"A": {"max_users": 1000, "max_active_users": 1000}}
        s.add(EnforcementAction(              # the REAL suspend (older)
            id=1, reseller_id=r.id, action=EnforcementActionType.disable_users, dry_run=False,
            status=EnforcementActionStatus.done,
            created_at=dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc),
            snapshot={"limits": real_limits, "admins": ["A"], "users": {"u1": "A", "u2": "A"},
                      "progress": _progress(users_done=["u1", "u2"], admins_done=["A"],
                                            captured_limits=real_limits)},
        ))
        s.add(EnforcementAction(              # the users-only RE-ASSERT (newer)
            id=2, reseller_id=r.id, action=EnforcementActionType.disable_users, dry_run=False,
            status=EnforcementActionStatus.done,
            created_at=dt.datetime(2026, 7, 24, tzinfo=dt.timezone.utc),
            snapshot={"limits": {}, "admins": [], "users": {"u1": "A", "u3": "A"},
                      "reassert": True,
                      "progress": _progress(users_done=["u1", "u3"])},
        ))
        await s.commit()

        restore = await enforcement.queue_restore(s, r, reason="panel")
        assert restore is not None
        snap = restore.snapshot

        # Limits come from the REAL suspend, not the empty re-assert.
        assert snap["limits"] == real_limits
        assert snap["admins"] == ["A"]
        assert snap["source_action_id"] == 1
        # The re-assert is absorbed, not used as the source…
        assert snap["absorbed_action_ids"] == [2]
        # …and every user either action disabled is re-enabled (u3 only exists on the re-assert).
        assert set(snap["users"]) == {"u1", "u2", "u3"}

    _run(body, tmp_path, "src.db")


def test_restore_falls_back_to_snapshot_when_only_reasserts_survive(tmp_path):
    """If the real suspend was pruned and only a re-assert remains, the restore still targets the
    admins that hold a recovery snapshot so the limits phase can put `max_users_snapshot` back —
    rather than restoring users and no limits at all."""

    async def body(s, Session):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R", max_users_snapshot=500,
                     max_active_users_snapshot=500,
                     enforcement_state=EnforcementState.enforced)
        s.add(r)
        await s.flush()
        s.add(EnforcementAction(
            id=1, reseller_id=r.id, action=EnforcementActionType.disable_users, dry_run=False,
            status=EnforcementActionStatus.done,
            snapshot={"limits": {}, "admins": [], "users": {"u1": "A"}, "reassert": True,
                      "progress": _progress(users_done=["u1"])},
        ))
        await s.commit()

        restore = await enforcement.queue_restore(s, r, reason="panel")
        assert restore is not None
        # The admin is included even though the source carried no limits, so `_run_admin_limits`
        # can recover 500/500 from the reseller's own snapshot.
        assert restore.snapshot["admins"] == ["A"]

    _run(body, tmp_path, "fallback.db")


# ── Bug 1b: never clear a recovery snapshot that was not actually restored ────

def test_restore_keeps_snapshot_of_admins_it_did_not_restore(tmp_path, monkeypatch):
    """A restore that re-applied NO limits for an admin must leave `max_users_snapshot` intact —
    clearing it destroyed the last copy of the real limits and poisoned every later cycle."""

    async def body(s, Session):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R", max_users_snapshot=1000,
                     max_active_users_snapshot=1000,
                     enforcement_state=EnforcementState.enforced)
        s.add(r)
        await s.flush()
        # A restore whose work set has NO admins/limits (the users-only shape).
        action = EnforcementAction(
            reseller_id=r.id, action=EnforcementActionType.restore, dry_run=False,
            status=EnforcementActionStatus.planned,
            snapshot={"limits": {}, "admins": [], "users": {"u1": "A"},
                      "progress": _progress(phase="limits")},
        )
        s.add(action)
        s.add(EndUserSnapshot(panel_id=1, user_uuid="u1", name="u1", added_by_uuid="A",
                              enable=False))
        await s.commit()

        async def fake_user_id(self, panel, user_uuid, *, api_key=None):
            return 10

        async def fake_bulk(self, panel, user_ids, enabled, *, api_key=None):
            return None

        async def fake_verify(session, action, client, panel, uuids, *, expect_enabled):
            return False

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_id", fake_user_id)
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
        monkeypatch.setattr(enforcement, "_fail_if_writes_missed", fake_verify)

        await enforcement._process_restore_action(
            s, action, user_chunk_size=100, admin_parallelism=2)
        await s.refresh(r)

        assert r.enforcement_state == EnforcementState.active   # users are back
        # …but the recovery snapshot survives, because no limit was ever re-applied.
        assert r.max_users_snapshot == 1000
        assert r.max_active_users_snapshot == 1000

    _run(body, tmp_path, "keepsnap.db")


# ── Bug 2: a bare 0/0 read must not be recorded as the real limits ────────────

def test_zero_limits_without_snapshot_are_not_captured(tmp_path, monkeypatch):
    """An admin already at 0/0 with no snapshot: recording 0 would make every future restore hand
    back 0. The admin is completed (nothing to write) but no limits are stored, and it is flagged."""

    async def body(s, Session):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R", panel_max_users=0,
                     panel_max_active_users=0, enforcement_state=EnforcementState.active)
        s.add(r)
        await s.flush()
        action = EnforcementAction(
            reseller_id=r.id, action=EnforcementActionType.disable_users, dry_run=False,
            status=EnforcementActionStatus.planned, snapshot={},
        )
        s.add(action)
        await s.commit()

        wrote: list = []

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            return (0, 0)                       # the panel already reads zero

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            wrote.append((admin_uuid, mu, mau))

        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        snapshot: dict = {"limits": {}, "admins": ["A"], "users": {}}
        progress = enforcement._progress(snapshot)
        captured: dict = {}
        done: set = set()
        patched, had_error = await enforcement._run_admin_limits(
            session=s, action=action, client=enforcement.AdminApiClient(), panel=None,
            snapshot=snapshot, progress=progress, by_uuid={"A": r}, admin_order=["A"],
            done_admins=done, failed_admins={}, admin_attempts={}, captured_limits=captured,
            is_suspend=True, parallelism=1,
        )

        assert not had_error                       # the suspension is not blocked
        assert "A" in done                         # nothing left to apply (already 0/0)
        assert captured == {}                      # the bogus zero was NOT recorded
        assert wrote == []                         # and re-zeroing was skipped
        assert progress["admins_zero_capture"] == ["A"]   # surfaced for the owner alert
        await s.refresh(r)
        assert r.max_users_snapshot is None        # no phantom snapshot written

    _run(body, tmp_path, "zerocap.db")


def test_real_limits_are_still_captured_normally(tmp_path, monkeypatch):
    """Control: a normal admin with real limits is captured and zeroed exactly as before."""

    async def body(s, Session):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R", panel_max_users=100,
                     panel_max_active_users=100, enforcement_state=EnforcementState.active)
        s.add(r)
        await s.flush()
        action = EnforcementAction(
            reseller_id=r.id, action=EnforcementActionType.disable_users, dry_run=False,
            status=EnforcementActionStatus.planned, snapshot={},
        )
        s.add(action)
        await s.commit()

        wrote: list = []

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            return (100, 100)

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            wrote.append((admin_uuid, mu, mau))

        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        snapshot: dict = {"limits": {}, "admins": ["A"], "users": {}}
        progress = enforcement._progress(snapshot)
        captured: dict = {}
        await enforcement._run_admin_limits(
            session=s, action=action, client=enforcement.AdminApiClient(), panel=None,
            snapshot=snapshot, progress=progress, by_uuid={"A": r}, admin_order=["A"],
            done_admins=set(), failed_admins={}, admin_attempts={}, captured_limits=captured,
            is_suspend=True, parallelism=1,
        )
        assert captured == {"A": {"max_users": 100, "max_active_users": 100}}
        assert wrote == [("A", 0, 0)]
        await s.refresh(r)
        assert r.max_users_snapshot == 100

    _run(body, tmp_path, "realcap.db")


# ── Bug 5: cascade state + ancestor-aware restore ────────────────────────────

async def _seed_tree(s):
    """Owner → parent (P) → child (C), each with one enabled user, parent owing an invoice."""
    s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
    owner = Reseller(panel_id=1, admin_uuid="owner", name="Owner", is_owner=True,
                     enforcement_state=EnforcementState.active)
    parent = Reseller(panel_id=1, admin_uuid="P", name="Parent", parent_admin_uuid="owner",
                      panel_max_users=100, panel_max_active_users=100,
                      enforcement_state=EnforcementState.active)
    child = Reseller(panel_id=1, admin_uuid="C", name="Child", parent_admin_uuid="P",
                     panel_max_users=50, panel_max_active_users=50,
                     enforcement_state=EnforcementState.active)
    s.add_all([owner, parent, child])
    await s.flush()
    s.add(EndUserSnapshot(panel_id=1, user_uuid="up", name="up", added_by_uuid="P", enable=True))
    s.add(EndUserSnapshot(panel_id=1, user_uuid="uc", name="uc", added_by_uuid="C", enable=True))
    s.add(_owed_invoice(parent.id))
    await s.commit()
    return parent, child


def test_suspend_marks_descendants_and_blocks_their_independent_restore(tmp_path, monkeypatch):
    """Suspending the parent must mark the child suspended in the DB (so the panel and the
    storefront guard can see it) and must stop the child being un-suspended on its own while the
    parent's debt stands. The parent's own restore still cascades back down to the child."""

    async def body(s, Session):
        parent, child = await _seed_tree(s)

        async def fake_user_id(self, panel, user_uuid, *, api_key=None):
            return {"up": 1, "uc": 2}[user_uuid]

        async def fake_bulk(self, panel, user_ids, enabled, *, api_key=None):
            return None

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            return {"P": (100, 100), "C": (50, 50)}[admin_uuid]

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            return None

        async def fake_verify(session, action, client, panel, uuids, *, expect_enabled):
            return False

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_id", fake_user_id)
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)
        monkeypatch.setattr(enforcement, "_fail_if_writes_missed", fake_verify)

        action = await enforcement.queue_enforcement(s, parent, dry_run=False)
        await enforcement._process_enforcement_action(
            s, action, user_chunk_size=100, admin_parallelism=2)
        await s.refresh(parent)
        await s.refresh(child)

        assert parent.enforcement_state == EnforcementState.enforced
        # The cascade is now visible in the database, not just on the panel.
        assert child.enforcement_state == EnforcementState.enforced

        # The child cannot be re-opened on its own while the parent is still suspended.
        assert await enforcement.queue_restore(s, child, reason="bot") is None

        # The parent's restore, however, still covers the whole subtree.
        restore = await enforcement.queue_restore(s, parent, reason="panel")
        assert restore is not None
        assert set(restore.snapshot["admins"]) == {"P", "C"}
        await enforcement._process_restore_action(
            s, restore, user_chunk_size=100, admin_parallelism=2)
        await s.refresh(parent)
        await s.refresh(child)
        assert parent.enforcement_state == EnforcementState.active
        assert child.enforcement_state == EnforcementState.active   # cascade lifted too

    _run(body, tmp_path, "cascade.db")


def test_independently_frozen_child_is_left_alone_by_parent_restore(tmp_path):
    """A child restricted by its OWN action keeps that restriction when the parent is restored —
    independence is judged by owning a live action, not by state alone."""

    async def body(s, Session):
        parent, child = await _seed_tree(s)
        child.enforcement_state = EnforcementState.frozen
        child.max_users_snapshot = 50
        s.add(EnforcementAction(          # the child's OWN freeze
            reseller_id=child.id, action=EnforcementActionType.freeze, dry_run=False,
            status=EnforcementActionStatus.done,
            snapshot={"limits": {"C": {"max_users": 50, "max_active_users": 50}},
                      "admins": ["C"], "users": {}, "progress": _progress(admins_done=["C"])},
        ))
        await s.commit()

        descendants = await enforcement._bundle(s, parent)
        independent = await enforcement._independently_restricted(s, parent, descendants)
        assert independent == {"C"}       # owns a live action → excluded from the parent's restore

    _run(body, tmp_path, "indep.db")


def test_cascade_child_without_own_action_is_not_independent(tmp_path):
    """The regression guard for the fix above: a child that is `enforced` purely because its parent
    was suspended owns no action, so the parent's restore MUST still cover it. Judging by state
    alone would make a suspended tree impossible to restore."""

    async def body(s, Session):
        parent, child = await _seed_tree(s)
        child.enforcement_state = EnforcementState.enforced   # cascade mark, no action of its own
        await s.commit()

        descendants = await enforcement._bundle(s, parent)
        assert await enforcement._independently_restricted(s, parent, descendants) == set()

    _run(body, tmp_path, "cascade2.db")


def test_enforced_ancestor_walk_survives_a_parent_cycle(tmp_path):
    """A corrupt hierarchy (A→B→A) must terminate, not spin."""

    async def body(s, Session):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        a = Reseller(panel_id=1, admin_uuid="A", name="A", parent_admin_uuid="B",
                     enforcement_state=EnforcementState.active)
        b = Reseller(panel_id=1, admin_uuid="B", name="B", parent_admin_uuid="A",
                     enforcement_state=EnforcementState.active)
        s.add_all([a, b])
        await s.commit()
        assert await enforcement._enforced_ancestor(s, a) is None

    _run(body, tmp_path, "cycle.db")


# ── Bug 3: the storefront must not write to the panel for a suspended branch ──

async def _seed_shop(s, *, state: EnforcementState, parent_state=EnforcementState.active):
    """A shop owned by reseller C, whose parent P may itself be suspended."""
    from app.core import crypto
    from app.models import StorefrontBot, StorefrontCustomer, StorefrontOrder

    s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
    parent = Reseller(panel_id=1, admin_uuid="P", name="Parent", parent_admin_uuid="owner",
                      enforcement_state=parent_state)
    s.add(parent)
    shop_owner = Reseller(panel_id=1, admin_uuid="C", name="Shop", parent_admin_uuid="P",
                          enforcement_state=state)
    s.add(shop_owner)
    await s.flush()
    bot = StorefrontBot(reseller_id=shop_owner.id, panel_id=1,
                        bot_token_enc=crypto.encrypt("123:abc") or "",
                        bot_telegram_id=9911, enabled=True)
    s.add(bot)
    await s.flush()
    cust = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=555)
    s.add(cust)
    await s.flush()
    order = StorefrontOrder(customer_id=cust.id, panel_id=1, plan_id=None, gb=10, days=30,
                            price_toman=1000, status="disabled", panel_user_uuid="pu",
                            is_trial=False)
    s.add(order)
    await s.commit()
    return bot, cust, order


def test_storefront_resume_refused_while_reseller_suspended(tmp_path, monkeypatch):
    """The confirmed live bypass: a customer could RESUME a paused config from the shop bot and the
    panel user came back online, with no enforcement record. It must be refused instead."""

    async def body(s, Session):
        from app.services import storefront_subscription

        _bot, _cust, order = await _seed_shop(s, state=EnforcementState.enforced)

        wrote: list = []

        async def fake_set_user_enabled(self, panel, uuid, enabled, *, api_key=None):
            wrote.append((uuid, enabled))

        monkeypatch.setattr(storefront_subscription.AdminApiClient, "set_user_enabled",
                            fake_set_user_enabled)

        res = await storefront_subscription.set_enabled(
            Session, order_id=order.id, enabled=True)
        assert res.ok is False and res.reason == "suspended"
        assert wrote == []          # nothing reached the panel

    _run(body, tmp_path, "sfresume.db")


def test_storefront_resume_refused_when_only_the_parent_is_suspended(tmp_path, monkeypatch):
    """Ancestor-aware: the shop's own reseller is active, but the branch above it is suspended."""

    async def body(s, Session):
        from app.services import storefront_subscription

        _bot, _cust, order = await _seed_shop(
            s, state=EnforcementState.active, parent_state=EnforcementState.enforced)

        wrote: list = []

        async def fake_set_user_enabled(self, panel, uuid, enabled, *, api_key=None):
            wrote.append((uuid, enabled))

        monkeypatch.setattr(storefront_subscription.AdminApiClient, "set_user_enabled",
                            fake_set_user_enabled)

        res = await storefront_subscription.set_enabled(
            Session, order_id=order.id, enabled=True)
        assert res.ok is False and res.reason == "suspended"
        assert wrote == []

    _run(body, tmp_path, "sfparent.db")


def test_storefront_pause_still_allowed_while_suspended(tmp_path, monkeypatch):
    """Pausing only ever takes a user OFF, so it must remain available — the guard is about
    re-enabling, not about freezing the customer out of their own controls."""

    async def body(s, Session):
        from app.services import storefront_subscription

        _bot, _cust, order = await _seed_shop(s, state=EnforcementState.enforced)
        async with Session() as s2:
            o = await s2.get(type(order), order.id)
            o.status = "provisioned"
            await s2.commit()

        wrote: list = []

        async def fake_set_user_enabled(self, panel, uuid, enabled, *, api_key=None):
            wrote.append((uuid, enabled))

        monkeypatch.setattr(storefront_subscription.AdminApiClient, "set_user_enabled",
                            fake_set_user_enabled)

        res = await storefront_subscription.set_enabled(
            Session, order_id=order.id, enabled=False)
        assert res.ok is True
        assert wrote == [("pu", False)]

    _run(body, tmp_path, "sfpause.db")


def test_storefront_renew_refused_before_any_charge(tmp_path, monkeypatch):
    """A renewal PATCHes the panel user with enable=True, so it must be refused for a suspended
    branch — and refused BEFORE the wallet is touched, so nobody pays for a shop that can't deliver."""

    async def body(s, Session):
        from app.services import storefront_subscription

        _bot, _cust, order = await _seed_shop(s, state=EnforcementState.enforced)

        async def boom(*a, **k):
            raise AssertionError("a suspended shop must not reach the panel or the wallet")

        monkeypatch.setattr(storefront_subscription.AdminApiClient, "prepare_renew_user", boom)

        res = await storefront_subscription.renew(Session, order_id=order.id)
        assert res.ok is False and res.reason == "suspended"

    _run(body, tmp_path, "sfrenew.db")


def test_storefront_purchase_and_trial_refused_while_suspended(tmp_path, monkeypatch):
    """Selling is blocked too: provisioning a fresh ENABLED user under a suspended reseller would
    hand back exactly the access enforcement just removed. The trial flag must not be burned."""

    async def body(s, Session):
        from app.models import StorefrontCustomer, StorefrontPlan
        from app.services import storefront_provision

        bot, cust, _order = await _seed_shop(s, state=EnforcementState.enforced)
        bot_id, cust_id = bot.id, cust.id
        async with Session() as s2:
            b = await s2.get(type(bot), bot_id)
            b.free_trial_enabled = True
            b.free_trial_gb, b.free_trial_days = 1, 1
            plan = StorefrontPlan(storefront_bot_id=bot_id, gb=10, days=30, price_toman=1000,
                                  enabled=True, sort_order=1)
            s2.add(plan)
            await s2.commit()
            plan_id = plan.id

        async def boom(*a, **k):
            raise AssertionError("a suspended shop must not provision")

        monkeypatch.setattr(storefront_provision, "provision", boom)

        buy = await storefront_provision.purchase(
            Session, sf_id=bot_id, customer_id=cust_id, plan_id=plan_id, label="x")
        assert buy.ok is False and buy.reason == "suspended"

        trial = await storefront_provision.claim_trial(
            Session, sf_id=bot_id, customer_id=cust_id)
        assert trial.ok is False and trial.reason == "suspended"
        async with Session() as s3:
            c = await s3.get(StorefrontCustomer, cust_id)
            assert c.free_trial_used is False   # the refused attempt didn't burn the trial

    _run(body, tmp_path, "sfbuy.db")


def test_storefront_works_normally_when_not_suspended(tmp_path, monkeypatch):
    """Control: an active reseller's shop is untouched by the guard."""

    async def body(s, Session):
        from app.services import storefront_subscription

        _bot, _cust, order = await _seed_shop(s, state=EnforcementState.active)

        wrote: list = []

        async def fake_set_user_enabled(self, panel, uuid, enabled, *, api_key=None):
            wrote.append((uuid, enabled))

        monkeypatch.setattr(storefront_subscription.AdminApiClient, "set_user_enabled",
                            fake_set_user_enabled)

        res = await storefront_subscription.set_enabled(
            Session, order_id=order.id, enabled=True)
        assert res.ok is True
        assert wrote == [("pu", True)]

    _run(body, tmp_path, "sfok.db")
