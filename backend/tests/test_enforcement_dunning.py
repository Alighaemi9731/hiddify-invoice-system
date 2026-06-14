"""Enforcement & reminder consistency (B05).

- All user chunks and admin limits complete in one worker tick (no artificial batching).
- Admin limits are patched in parallel (asyncio.gather + Semaphore).
- A failed chunk leaves the action partial so the next tick retries only the remainder.
- A partial restore (some users fail) leaves the reseller ENFORCED until fully complete.
- A pending-payment hold is scoped to its invoice and expires after pending_payment_hold_days.
- Reminder reports distinguish attempted from delivered.
- The GB-cap once-per-month flag is armed only after the alert reaches every recipient.
"""
import asyncio
import datetime as dt
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/enf.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import (  # noqa: E402
    EndUserSnapshot,
    EnforcementAction,
    Invoice,
    Panel,
    Payment,
    Reseller,
)
from app.models.enums import (  # noqa: E402
    DeliveryStatus,
    EnforcementActionStatus,
    EnforcementActionType,
    EnforcementState,
    InvoiceStatus,
    PaymentMethod,
    PaymentStatus,
)


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


def _invoice(reseller_id, *, label, status=InvoiceStatus.sent, sent_days_ago=3):
    y, m = (int(x) for x in label.split("-"))
    start = dt.date(y, m, 1)
    end = dt.date(y + (m // 12), (m % 12) + 1, 1) - dt.timedelta(days=1)
    return Invoice(
        reseller_id=reseller_id, panel_id=1, period_start=start, period_end=end,
        period_label=label, usage_gb=10, amount_toman=10000, amount_usdt=1, status=status,
        sent_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=sent_days_ago),
    )


# ── suspend: all chunks + admin limits complete in ONE tick ──────────────────

def test_enforcement_completes_in_one_tick(tmp_path, monkeypatch):
    """All user chunks and admin limits are processed in a single worker invocation."""
    from app.services import enforcement, settings_service

    async def body(s):
        await settings_service.set_value(s, "enforcement_enabled", True)
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R", panel_max_users=10,
                     panel_max_active_users=10, enforcement_state=EnforcementState.active)
        s.add(r)
        await s.flush()
        for i in range(3):
            s.add(EndUserSnapshot(panel_id=1, user_uuid=f"u{i}", name=f"u{i}",
                                  added_by_uuid="A", enable=True))
        invoice = _invoice(r.id, label="2026-03", sent_days_ago=5)
        s.add(invoice)
        await s.commit()

        action = await enforcement.queue_enforcement(
            s, r, invoice_id=invoice.id, dry_run=False
        )
        assert action.status == EnforcementActionStatus.planned

        calls: list[tuple] = []

        async def fake_user_ids(self, panel):
            return {"u0": 10, "u1": 11, "u2": 12}

        async def fake_bulk(self, panel, user_ids, enabled):
            calls.append(("bulk", tuple(sorted(user_ids)), enabled))

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            calls.append(("get_limits", admin_uuid))
            return (10, 10)

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            calls.append(("set_limits", admin_uuid, mu, mau))

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_ids", fake_user_ids)
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        # ONE tick with chunk_size=2: processes chunk [u0,u1], chunk [u2], then admin A.
        res = await enforcement.process_enforcement_queue(s, action_limit=1, user_chunk_size=2)
        assert res["done"] == 1, f"expected done in one tick, got: {res}"
        await s.refresh(action)
        assert action.status == EnforcementActionStatus.done
        assert action.affected_count == 3

        # Both user chunks were sent to bulk disable.
        bulk_calls = [c for c in calls if c[0] == "bulk"]
        assert len(bulk_calls) == 2
        all_disabled = {uid for _, ids, _ in bulk_calls for uid in ids}
        assert all_disabled == {10, 11, 12}
        assert all(enabled is False for _, _, enabled in bulk_calls)

        # Admin A limits were captured and zeroed.
        assert ("get_limits", "A") in calls
        assert ("set_limits", "A", 0, 0) in calls

        await s.refresh(r)
        assert r.enforcement_state == EnforcementState.enforced
        assert r.max_users_snapshot == 10

    _run(body, tmp_path, "one_tick.db")


def test_enforcement_partial_on_user_chunk_failure(tmp_path, monkeypatch):
    """When a user chunk fails, action stays partial and retries only the failed chunk."""
    from app.services import enforcement, settings_service

    async def body(s):
        await settings_service.set_value(s, "enforcement_enabled", True)
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R", panel_max_users=10,
                     panel_max_active_users=10, enforcement_state=EnforcementState.active)
        s.add(r)
        await s.flush()
        for i in range(3):
            s.add(EndUserSnapshot(panel_id=1, user_uuid=f"u{i}", name=f"u{i}",
                                  added_by_uuid="A", enable=True))
        s.add(_invoice(r.id, label="2026-03", sent_days_ago=5))
        await s.commit()

        action = await enforcement.queue_enforcement(s, r, dry_run=False)

        call_count = [0]

        async def fake_user_ids(self, panel):
            return {"u0": 10, "u1": 11, "u2": 12}

        async def fake_bulk(self, panel, user_ids, enabled):
            call_count[0] += 1
            if call_count[0] == 2:  # second chunk fails
                raise RuntimeError("panel error")

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            return (10, 10)

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            pass

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_ids", fake_user_ids)
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        # First tick: chunk [u0,u1] succeeds, chunk [u2] fails → partial.
        res1 = await enforcement.process_enforcement_queue(s, action_limit=1, user_chunk_size=2)
        assert res1["partial"] == 1
        await s.refresh(action)
        assert action.status == EnforcementActionStatus.partial
        assert action.affected_count == 2  # only u0 and u1 confirmed
        assert r.enforcement_state == EnforcementState.active

        # Second tick: u2 now succeeds, then admin limits run → done.
        res2 = await enforcement.process_enforcement_queue(s, action_limit=1, user_chunk_size=2)
        assert res2["done"] == 1
        await s.refresh(r)
        assert r.enforcement_state == EnforcementState.enforced

    _run(body, tmp_path, "partial_chunk.db")


# ── restore: all admins in parallel, all user chunks in one tick ─────────────

def test_restore_completes_admin_limits_in_parallel_one_tick(tmp_path, monkeypatch):
    """All admin limits are restored in one worker tick via asyncio.gather."""
    from app.services import enforcement

    async def body(s):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        root = Reseller(panel_id=1, admin_uuid="A", name="Root",
                        enforcement_state=EnforcementState.enforced,
                        max_users_snapshot=100, max_active_users_snapshot=90)
        child = Reseller(panel_id=1, admin_uuid="B", parent_admin_uuid="A",
                         name="Child", enforcement_state=EnforcementState.active,
                         max_users_snapshot=50, max_active_users_snapshot=40)
        s.add_all([root, child])
        await s.flush()
        source = EnforcementAction(
            reseller_id=root.id, invoice_id=None,
            action=EnforcementActionType.disable_users, dry_run=False,
            status=EnforcementActionStatus.done,
            snapshot={
                "admins": ["A", "B"],
                "limits": {
                    "A": {"max_users": 100, "max_active_users": 90},
                    "B": {"max_users": 50, "max_active_users": 40},
                },
                "users": {},
            },
        )
        s.add(source)
        await s.commit()

        calls: list[tuple] = []

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            calls.append((admin_uuid, mu, mau))

        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        restore = await enforcement.queue_restore(s, root, reason="test")
        assert restore is not None

        # ONE tick restores BOTH admins (parallel, not one-per-tick).
        result = await enforcement.process_enforcement_queue(
            s, action_limit=1, user_chunk_size=100, admin_chunk_size=10
        )
        assert result["done"] == 1, f"expected done in one tick, got: {result}"

        # Both admin limits were restored with correct values.
        assert ("A", 100, 90) in calls
        assert ("B", 50, 40) in calls

        await s.refresh(root)
        await s.refresh(child)
        assert root.enforcement_state == EnforcementState.active
        assert root.max_users_snapshot is None
        assert child.max_users_snapshot is None

    _run(body, tmp_path, "restore_parallel.db")


# ── partial restore keeps reseller enforced (retryable) ──────────────────────

def test_partial_restore_keeps_reseller_enforced(tmp_path, monkeypatch):
    from app.services import enforcement, settings_service

    async def body(s):
        await settings_service.set_value(s, "enforcement_user_chunk_size", 1)
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R",
                     enforcement_state=EnforcementState.enforced, max_users_snapshot=100)
        s.add(r)
        await s.flush()
        s.add(EnforcementAction(
            reseller_id=r.id, action=EnforcementActionType.disable_users,
            dry_run=False, status=EnforcementActionStatus.done, affected_count=2,
            snapshot={"limits": {"A": {"max_users": 100, "max_active_users": 100}},
                      "users": {"u1": "A", "u2": "A"}},
        ))
        await s.commit()

        fail_uuid = {"u": "u2"}

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            return None

        async def fake_user_ids(self, panel):
            return {"u1": 1, "u2": 2}

        async def fake_bulk(self, panel, user_ids, enabled):
            if 2 in user_ids and fail_uuid["u"] == "u2":
                raise RuntimeError("panel rejected")

        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_ids", fake_user_ids)
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)

        res = await enforcement.restore_reseller(s, r)
        assert res.status == EnforcementActionStatus.planned

        # Tick 1: admin limits restored (success), u1 enabled (success), u2 fails → partial.
        first = await enforcement.process_enforcement_queue(
            s, action_limit=1, user_chunk_size=1, admin_chunk_size=1
        )
        assert first["partial"] == 1
        assert first["restored_users"] == 1
        assert r.enforcement_state == EnforcementState.enforced
        assert r.max_users_snapshot == 100  # snapshot NOT cleared yet

        # Tick 2: u2 still fails → partial.
        second = await enforcement.process_enforcement_queue(
            s, action_limit=1, user_chunk_size=1, admin_chunk_size=1
        )
        assert second["partial"] == 1
        await s.refresh(res)
        assert res.status == EnforcementActionStatus.partial
        assert r.enforcement_state == EnforcementState.enforced

        # Tick 3: u2 succeeds → done, reseller active.
        fail_uuid["u"] = "none"
        third = await enforcement.process_enforcement_queue(
            s, action_limit=1, user_chunk_size=1, admin_chunk_size=1
        )
        assert third["done"] == 1
        await s.refresh(res)
        assert res.status == EnforcementActionStatus.done
        assert r.enforcement_state == EnforcementState.active
        assert r.max_users_snapshot is None

    _run(body, tmp_path, "enf.db")


# ── restore cancels a partial disable and only undoes completed work ──────────

def test_restore_cancels_partial_disable_and_only_undoes_completed_work(
    tmp_path, monkeypatch
):
    from app.services import enforcement, settings_service

    async def body(s):
        await settings_service.set_value(s, "enforcement_enabled", True)
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R",
                     panel_max_users=10, panel_max_active_users=10,
                     enforcement_state=EnforcementState.active)
        s.add(r)
        await s.flush()
        for i in range(3):
            s.add(EndUserSnapshot(panel_id=1, user_uuid=f"u{i}", name=f"u{i}",
                                  added_by_uuid="A", enable=True))
        invoice = _invoice(r.id, label="2026-04", sent_days_ago=5)
        s.add(invoice)
        await s.commit()

        calls: list[tuple] = []
        call_count = [0]

        async def fake_user_ids(self, panel):
            return {"u0": 10, "u1": 11, "u2": 12}

        async def fake_bulk(self, panel, user_ids, enabled):
            call_count[0] += 1
            if call_count[0] == 2:
                # Second bulk call (second chunk during disable) fails, creating a partial.
                raise RuntimeError("panel error during disable")
            calls.append((enabled, tuple(sorted(user_ids))))

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_ids", fake_user_ids)
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)

        disable = await enforcement.queue_enforcement(
            s, r, invoice_id=invoice.id, dry_run=False
        )
        # chunk_size=2: [u0,u1] succeeds, [u2] fails → partial, admin limits not reached.
        await enforcement.process_enforcement_queue(s, action_limit=1, user_chunk_size=2)
        await s.refresh(disable)
        assert disable.status == EnforcementActionStatus.partial
        assert calls == [(False, (10, 11))]  # only first chunk was applied

        # Payment arrives: queue restore while disable is only partially done.
        invoice.status = InvoiceStatus.paid
        restore = await enforcement.queue_restore(s, r, require_no_due=True, reason="payment")
        assert restore is not None
        await s.refresh(disable)
        assert disable.status == EnforcementActionStatus.reverted

        # Restore: only u0 and u1 (the two actually disabled) get re-enabled.
        result = await enforcement.process_enforcement_queue(
            s, action_limit=1, user_chunk_size=100, admin_chunk_size=10
        )
        assert result["done"] == 1
        await s.refresh(r)
        assert r.enforcement_state == EnforcementState.active
        assert calls[-1] == (True, (10, 11))   # restore only what was disabled
        assert 12 not in calls[-1][1]           # u2 was never disabled → not restored

    _run(body, tmp_path, "cancel_partial.db")


# ── other invariants ─────────────────────────────────────────────────────────

def test_pending_hold_is_per_invoice_and_expires(tmp_path):
    from app.services import dunning

    async def body(s):
        now = dt.datetime.now(dt.timezone.utc)
        r = Reseller(panel_id=1, admin_uuid="A", name="R",
                     enforcement_state=EnforcementState.active, bot_chat_id=None)
        s.add(r)
        await s.flush()
        inv_a = _invoice(r.id, label="2026-01")
        inv_b = _invoice(r.id, label="2026-02")
        s.add_all([inv_a, inv_b])
        await s.flush()
        s.add(Payment(reseller_id=r.id, invoice_id=inv_a.id, method=PaymentMethod.screenshot,
                      status=PaymentStatus.pending, created_at=now))
        s.add(Payment(reseller_id=r.id, invoice_id=inv_b.id, method=PaymentMethod.screenshot,
                      status=PaymentStatus.pending,
                      created_at=now - dt.timedelta(days=30)))
        await s.commit()

        res = await dunning.run_dunning(s, now=now)
        assert res["on_hold"] == 1
        assert res["reminder1"] == 1
        assert res["reminder1_sent"] == 0

    _run(body, tmp_path, "dun.db")


def test_dunning_queues_live_enforcement_instead_of_blocking(tmp_path):
    from app.services import dunning, settings_service

    async def body(s):
        now = dt.datetime.now(dt.timezone.utc)
        await settings_service.set_value(s, "enforcement_enabled", True)
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R", bot_chat_id=123,
                     enforcement_state=EnforcementState.active)
        s.add(r)
        await s.flush()
        s.add(_invoice(r.id, label="2026-03", sent_days_ago=5))
        s.add(EndUserSnapshot(panel_id=1, user_uuid="u1", name="u1", added_by_uuid="A",
                              enable=True))
        await s.commit()

        res = await dunning.run_dunning(s, now=now)
        assert res["warning"] == 1
        assert res["enforcement_queued"] == 1
        assert res["enforced"] == 0
        await s.refresh(r)
        assert r.enforcement_state == EnforcementState.active
        action = (await s.execute(select(EnforcementAction))).scalar_one()
        assert action.status == EnforcementActionStatus.planned
        assert action.dry_run is False
        assert action.affected_count == 1

    _run(body, tmp_path, "queue.db")


def test_live_queue_is_not_blocked_by_prior_dry_run(tmp_path):
    from app.services import enforcement, settings_service

    async def body(s):
        await settings_service.set_value(s, "enforcement_enabled", True)
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R",
                     enforcement_state=EnforcementState.active)
        s.add(r)
        await s.flush()
        s.add(EndUserSnapshot(panel_id=1, user_uuid="u1", name="u1", added_by_uuid="A",
                              enable=True))
        await s.commit()

        dry = await enforcement.queue_enforcement(s, r, invoice_id=88, dry_run=True)
        live = await enforcement.queue_enforcement(s, r, invoice_id=88, dry_run=False)
        same_live = await enforcement.queue_enforcement(s, r, invoice_id=88, dry_run=False)

        assert dry.status == EnforcementActionStatus.dry_run
        assert dry.dry_run is True
        assert live.id != dry.id
        assert live.status == EnforcementActionStatus.planned
        assert live.dry_run is False
        assert same_live.id == live.id

        actions = (await s.execute(
            select(EnforcementAction).where(EnforcementAction.invoice_id == 88)
        )).scalars().all()
        assert len(actions) == 2

    _run(body, tmp_path, "queue_after_dry.db")


def test_completed_restore_moves_enforced_invoice_back_to_overdue(tmp_path, monkeypatch):
    from app.services import enforcement

    async def body(s):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R",
                     enforcement_state=EnforcementState.enforced,
                     max_users_snapshot=100, max_active_users_snapshot=100)
        s.add(r)
        await s.flush()
        invoice = _invoice(r.id, label="2026-05", status=InvoiceStatus.enforced,
                           sent_days_ago=8)
        s.add(invoice)
        await s.flush()
        s.add(EnforcementAction(
            reseller_id=r.id, invoice_id=invoice.id,
            action=EnforcementActionType.disable_users, dry_run=False,
            status=EnforcementActionStatus.done,
            snapshot={
                "admins": ["A"],
                "limits": {"A": {"max_users": 100, "max_active_users": 100}},
                "users": {},
            },
        ))
        await s.commit()

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            return None

        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        restore = await enforcement.queue_restore(s, r, reason="panel")
        assert restore is not None
        result = await enforcement.process_enforcement_queue(
            s, action_limit=1, user_chunk_size=100, admin_chunk_size=10
        )
        assert result["done"] == 1
        await s.refresh(invoice)
        await s.refresh(r)
        assert invoice.status == InvoiceStatus.overdue
        assert r.enforcement_state == EnforcementState.active

    _run(body, tmp_path, "restore_invoice_status.db")


def test_enforcement_never_zeros_limits_without_a_restore_snapshot(tmp_path, monkeypatch):
    from app.services import enforcement, settings_service

    async def body(s):
        await settings_service.set_value(s, "enforcement_enabled", True)
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R",
                     panel_max_users=None, panel_max_active_users=None,
                     enforcement_state=EnforcementState.active)
        s.add(r)
        await s.commit()

        writes: list[tuple] = []

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            return (None, None)

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            writes.append((mu, mau))

        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        action = await enforcement.queue_enforcement(s, r, dry_run=False)
        result = await enforcement.process_enforcement_queue(
            s, action_limit=1, user_chunk_size=100
        )
        await s.refresh(action)
        assert result["partial"] == 1
        assert action.status == EnforcementActionStatus.partial
        assert writes == []
        assert r.enforcement_state == EnforcementState.active

    _run(body, tmp_path, "unknown_limits.db")


# ── gb_cap flag armed only after delivery ────────────────────────────────────

def test_gb_cap_flag_only_after_delivery(tmp_path, monkeypatch):
    from app.services import gb_cap
    from app.services.periods import current_month

    async def body(s):
        parent = Reseller(panel_id=1, admin_uuid="P", name="Parent", bot_chat_id=456)
        sub = Reseller(panel_id=1, admin_uuid="S", name="Sub", parent_admin_uuid="P",
                       bot_chat_id=123, gb_cap=10, gb_cap_alerted_period=None)
        s.add_all([parent, sub])
        await s.commit()

        async def fake_billable(session, r):
            return 15.0

        monkeypatch.setattr(gb_cap.reseller_report, "current_billable_gb", fake_billable)

        sent_status = {parent.id: DeliveryStatus.failed, sub.id: DeliveryStatus.failed}

        async def fake_send(session, reseller, text, **kw):
            return SimpleNamespace(status=sent_status[reseller.id])

        import app.services.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "send_to_reseller", fake_send)

        res = await gb_cap.check_caps(s, bot=object())
        assert res["over"] == 1 and res["alerted"] == 0
        assert sub.gb_cap_alerted_period is None

        sent_status[sub.id] = DeliveryStatus.sent
        res2 = await gb_cap.check_caps(s, bot=object())
        assert res2["alerted"] == 0
        assert sub.gb_cap_alerted_period is None

        sent_status[parent.id] = DeliveryStatus.sent
        res3 = await gb_cap.check_caps(s, bot=object())
        assert res3["alerted"] == 1
        assert sub.gb_cap_alerted_period == current_month().label

    _run(body, tmp_path, "cap.db")
