"""Enforcement & reminder consistency (B05).

- A partial restore (some users fail to re-enable) must leave the reseller ENFORCED so the
  next trigger retries, never flip to active and strand disabled users.
- A pending-payment hold is scoped to ITS invoice and expires after pending_payment_hold_days
  — one proof can't pause dunning on unrelated debts forever.
- Reminder reports distinguish attempted from delivered.
- The GB-cap once-per-month flag is armed only after the alert reaches every configured recipient.
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


# --------------------------- partial restore stays enforced (retryable)
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

        async def fake_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            return None

        async def fake_user_ids(self, panel):
            return {"u1": 1, "u2": 2}

        async def fake_bulk(self, panel, user_ids, enabled):
            if 2 in user_ids and fail_uuid["u"] == "u2":
                raise RuntimeError("panel rejected")

        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_ids", fake_user_ids)
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)

        # First restore: u2 fails → must stay ENFORCED (retryable), snapshot kept.
        res = await enforcement.restore_reseller(s, r)
        assert res.status == EnforcementActionStatus.failed
        assert r.enforcement_state == EnforcementState.enforced
        assert r.max_users_snapshot == 100  # snapshot NOT cleared (needed for retry)

        # Retry with everything succeeding → now fully restored.
        fail_uuid["u"] = "none"
        res2 = await enforcement.restore_reseller(s, r)
        assert res2.status == EnforcementActionStatus.done
        assert r.enforcement_state == EnforcementState.active
        assert r.max_users_snapshot is None

    _run(body, tmp_path, "enf.db")


# --------------------------- per-invoice hold + expiry + attempted vs delivered
def test_pending_hold_is_per_invoice_and_expires(tmp_path):
    from app.services import dunning

    async def body(s):
        now = dt.datetime.now(dt.timezone.utc)
        # bot_chat_id=None → reminders are "attempted" but unmatched (not delivered).
        r = Reseller(panel_id=1, admin_uuid="A", name="R",
                     enforcement_state=EnforcementState.active, bot_chat_id=None)
        s.add(r)
        await s.flush()
        inv_a = _invoice(r.id, label="2026-01")   # held (fresh pending payment)
        inv_b = _invoice(r.id, label="2026-02")   # NOT held (pending payment is stale)
        s.add_all([inv_a, inv_b])
        await s.flush()
        s.add(Payment(reseller_id=r.id, invoice_id=inv_a.id, method=PaymentMethod.screenshot,
                      status=PaymentStatus.pending, created_at=now))
        s.add(Payment(reseller_id=r.id, invoice_id=inv_b.id, method=PaymentMethod.screenshot,
                      status=PaymentStatus.pending, created_at=now - dt.timedelta(days=30)))
        await s.commit()

        res = await dunning.run_dunning(s, now=now)
        assert res["on_hold"] == 1                 # invoice A only
        assert res["reminder1"] == 1               # invoice B attempted
        assert res["reminder1_sent"] == 0          # but not delivered (unmatched)

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


def test_enforcement_queue_processes_in_resumable_chunks(tmp_path, monkeypatch):
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
        await s.commit()

        action = await enforcement.queue_enforcement(s, r, invoice_id=77, dry_run=False)
        assert action.status == EnforcementActionStatus.planned

        calls: list[tuple[str, str | int]] = []

        async def fake_user_ids(self, panel):
            return {"u0": 10, "u1": 11, "u2": 12}

        async def fake_bulk(self, panel, user_ids, enabled):
            calls.append(("bulk", tuple(user_ids)))

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            calls.append(("get_limits", admin_uuid))
            return (10, 10)

        async def fake_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            calls.append(("limits", admin_uuid))

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_ids", fake_user_ids)
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_limits)

        res1 = await enforcement.process_enforcement_queue(s, action_limit=1, user_chunk_size=2)
        assert res1["partial"] == 1
        await s.refresh(action)
        assert action.status == EnforcementActionStatus.partial
        assert action.affected_count == 2
        assert ("bulk", (10, 11)) in calls

        res2 = await enforcement.process_enforcement_queue(s, action_limit=1, user_chunk_size=2)
        assert res2["partial"] == 1
        await s.refresh(action)
        assert action.affected_count == 3
        assert r.enforcement_state == EnforcementState.active
        assert ("bulk", (12,)) in calls

        res3 = await enforcement.process_enforcement_queue(s, action_limit=1, user_chunk_size=2)
        assert res3["done"] == 1
        await s.refresh(action)
        await s.refresh(r)
        assert action.status == EnforcementActionStatus.done
        assert action.affected_count == 3
        assert r.enforcement_state == EnforcementState.enforced
        assert ("limits", "A") in calls

    _run(body, tmp_path, "queue_chunks.db")


# --------------------------- gb_cap flag armed only after delivery
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
            return 15.0  # over the 10 GB cap

        monkeypatch.setattr(gb_cap.reseller_report, "current_billable_gb", fake_billable)

        sent_status = {
            parent.id: DeliveryStatus.failed,
            sub.id: DeliveryStatus.failed,
        }

        async def fake_send(session, reseller, text, **kw):
            return SimpleNamespace(status=sent_status[reseller.id])

        import app.services.notifier as notifier_mod
        monkeypatch.setattr(notifier_mod, "send_to_reseller", fake_send)

        # Delivery FAILS → the once-per-month flag must NOT be armed (retry next check).
        res = await gb_cap.check_caps(s, bot=object())
        assert res["over"] == 1 and res["alerted"] == 0
        assert sub.gb_cap_alerted_period is None

        # Only the sub succeeds → parent must still be retried, so no flag yet.
        sent_status[sub.id] = DeliveryStatus.sent
        res2 = await gb_cap.check_caps(s, bot=object())
        assert res2["alerted"] == 0
        assert sub.gb_cap_alerted_period is None

        # BOTH configured recipients succeed → flag armed.
        sent_status[parent.id] = DeliveryStatus.sent
        res3 = await gb_cap.check_caps(s, bot=object())
        assert res3["alerted"] == 1
        assert sub.gb_cap_alerted_period == current_month().label

    _run(body, tmp_path, "cap.db")
