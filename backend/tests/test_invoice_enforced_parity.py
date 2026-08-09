"""A suspended reseller's invoice list must read the same for every unpaid month.

The panel showed «مسدود» only on the single invoice an enforcement action was queued
against; the same reseller's other unpaid month sat on «سررسید گذشته»/«ارسال‌شده», which
reads as if only one month were blocked. Suspension is a RESELLER-level fact, so all four
paths that can produce the mismatch are pinned here:

  1. suspend      → every OWED invoice of the reseller is stamped (paid ones never are);
  2. delivery     → an invoice sent to an already-suspended reseller is born `enforced`;
  3. daily sweep  → `reassert_enforced` heals rows that predate the rule;
  4. restore      → each one goes back to its own dunning clock (overdue past the warning
                    day, sent before it) — never a blanket flip.
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/enfparity.db")
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


def _panel_fakes(monkeypatch, enforcement):
    async def fake_user_id(self, panel, user_uuid, *, api_key=None):
        return {"u0": 10, "u1": 11}.get(user_uuid)

    async def fake_bulk(self, panel, user_ids, enabled, *, api_key=None):
        return None

    async def fake_get_limits(self, panel, admin_uuid, api_key=None):
        return (10, 10)

    async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
        return None

    monkeypatch.setattr(enforcement.AdminApiClient, "get_user_identity", as_identity(fake_user_id))
    monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
    monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
    monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)


# ── 1. suspend stamps every owed month, never a paid one ─────────────────────

def test_suspend_stamps_all_owed_invoices(tmp_path, monkeypatch):
    from app.services import enforcement, settings_service

    async def body(s):
        await settings_service.set_value(s, "enforcement_enabled", True)
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R", panel_max_users=10,
                     panel_max_active_users=10, enforcement_state=EnforcementState.active)
        s.add(r)
        await s.flush()
        s.add(EndUserSnapshot(panel_id=1, user_uuid="u0", name="u0",
                              added_by_uuid="A", enable=True))
        may = _invoice(r.id, label="2026-05", sent_days_ago=40)      # triggers enforcement
        june = _invoice(r.id, label="2026-06", sent_days_ago=6)      # merely `sent`
        april = _invoice(r.id, label="2026-04", status=InvoiceStatus.paid, sent_days_ago=70)
        s.add_all([may, june, april])
        await s.commit()

        _panel_fakes(monkeypatch, enforcement)
        action = await enforcement.queue_enforcement(s, r, invoice_id=may.id, dry_run=False)
        res = await enforcement.process_enforcement_queue(s, action_limit=1)
        assert res["done"] == 1, res
        await s.refresh(action)
        assert action.status == EnforcementActionStatus.done

        for inv in (may, june, april):
            await s.refresh(inv)
        assert may.status == InvoiceStatus.enforced
        # The whole point: the month that did NOT trigger the action says «مسدود» too.
        assert june.status == InvoiceStatus.enforced
        # A settled month is never dragged back into debt.
        assert april.status == InvoiceStatus.paid

    _run(body, tmp_path, "stamp_all.db")


# ── 2. an invoice delivered to an already-suspended reseller ─────────────────

def test_invoice_delivered_to_suspended_reseller_is_born_enforced(tmp_path, monkeypatch):
    from app.services import delivery

    async def body(s):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        blocked = Reseller(panel_id=1, admin_uuid="A", name="Blocked", bot_chat_id=111,
                           enforcement_state=EnforcementState.enforced)
        normal = Reseller(panel_id=1, admin_uuid="B", name="Normal", bot_chat_id=222,
                          enforcement_state=EnforcementState.active)
        s.add_all([blocked, normal])
        await s.flush()
        inv_b = _invoice(blocked.id, label="2026-06", status=InvoiceStatus.draft)
        inv_b.sent_at = None
        inv_n = _invoice(normal.id, label="2026-06", status=InvoiceStatus.draft)
        inv_n.sent_at = None
        s.add_all([inv_b, inv_n])
        await s.commit()

        # Drive only the status branch: stub out the Telegram round-trip entirely.
        class _Session:
            async def close(self):
                return None

        class _Bot:
            session = _Session()

        async def fake_content(session, bot, chat_id, inv, reseller, *, text=""):
            return [1]

        monkeypatch.setattr(delivery, "build_bot", lambda session: _bot(_Bot()))
        monkeypatch.setattr(delivery, "send_invoice_content", fake_content)
        for inv, reseller in ((inv_b, blocked), (inv_n, normal)):
            await delivery.send_invoice(s, inv.id)
            await s.refresh(inv)

        assert inv_b.status == InvoiceStatus.enforced
        assert inv_n.status == InvoiceStatus.sent
        assert inv_b.sent_at is not None  # the dunning clock still starts normally

    _run(body, tmp_path, "delivered_enforced.db")


# ── 3. the daily sweep self-heals rows that predate the rule ─────────────────

def test_daily_sweep_self_heals_stale_invoice_status(tmp_path):
    """`reassert_enforced` runs daily over exactly the enforced resellers — it repairs display
    drift there, so no invoice is left behind by a path that stamped only one of them."""
    from app.services import enforcement

    async def body(s):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R", bot_chat_id=111,
                     enforcement_state=EnforcementState.enforced)
        s.add(r)
        await s.flush()
        stale = _invoice(r.id, label="2026-06", status=InvoiceStatus.overdue, sent_days_ago=9)
        deferred = _invoice(r.id, label="2026-07", status=InvoiceStatus.sent, sent_days_ago=1)
        deferred.deferred_until = dt.date.today() + dt.timedelta(days=10)
        paid = _invoice(r.id, label="2026-05", status=InvoiceStatus.paid, sent_days_ago=40)
        s.add_all([stale, deferred, paid])
        await s.commit()

        out = await enforcement.reassert_enforced(s)
        assert out["restamped"] == 2
        for inv in (stale, deferred, paid):
            await s.refresh(inv)
        assert stale.status == InvoiceStatus.enforced
        # A payment deadline pauses the dunning clock — it does not un-block the reseller.
        assert deferred.status == InvoiceStatus.enforced
        assert paid.status == InvoiceStatus.paid

        # Idempotent: a second sweep changes nothing.
        assert (await enforcement.reassert_enforced(s))["restamped"] == 0

    _run(body, tmp_path, "sweep_heal.db")


async def _bot(obj):
    return obj


# ── 5. canceling the last debt lifts the suspension ──────────────────────────

def test_cancel_lifts_a_suspension_only_when_no_debt_remains(tmp_path):
    """«لغو فاکتور» removes debt, so it has to be able to un-block a reseller the way a payment
    does — otherwise voiding a debtor's only invoice leaves them suspended with nothing to pay."""
    from app.api.invoices import cancel
    from app.models import EnforcementAction as EA

    async def body(s):
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R",
                     enforcement_state=EnforcementState.enforced, max_users_snapshot=100)
        s.add(r)
        await s.flush()
        may = _invoice(r.id, label="2026-05", status=InvoiceStatus.enforced, sent_days_ago=40)
        june = _invoice(r.id, label="2026-06", status=InvoiceStatus.enforced, sent_days_ago=10)
        s.add_all([may, june])
        s.add(EnforcementAction(
            reseller_id=r.id, action=EnforcementActionType.disable_users,
            dry_run=False, status=EnforcementActionStatus.done, affected_count=1,
            snapshot={"limits": {"A": {"max_users": 100, "max_active_users": 100}},
                      "users": {"u0": "A"}},
        ))
        await s.commit()

        async def _restores():
            return (await s.execute(
                select(EA).where(EA.action == EnforcementActionType.restore)
            )).scalars().all()

        await cancel(may.id, s)
        await s.refresh(may)
        assert may.status == InvoiceStatus.canceled
        # June is still owed → the suspension stands.
        assert await _restores() == []

        await cancel(june.id, s)
        await s.refresh(june)
        assert june.status == InvoiceStatus.canceled
        assert len(await _restores()) == 1

    _run(body, tmp_path, "cancel_restore.db")


# ── 4. restore hands each invoice back to its OWN clock ──────────────────────

def test_restore_returns_each_invoice_to_its_own_dunning_clock(tmp_path, monkeypatch):
    from app.services import enforcement, settings_service

    async def body(s):
        await settings_service.set_value(s, "warning_day", 5)
        s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o"))
        r = Reseller(panel_id=1, admin_uuid="A", name="R",
                     enforcement_state=EnforcementState.enforced, max_users_snapshot=100)
        s.add(r)
        await s.flush()
        old = _invoice(r.id, label="2026-05", status=InvoiceStatus.enforced, sent_days_ago=40)
        fresh = _invoice(r.id, label="2026-06", status=InvoiceStatus.enforced, sent_days_ago=2)
        s.add_all([old, fresh])
        s.add(EnforcementAction(
            reseller_id=r.id, action=EnforcementActionType.disable_users,
            dry_run=False, status=EnforcementActionStatus.done, affected_count=1,
            snapshot={"limits": {"A": {"max_users": 100, "max_active_users": 100}},
                      "users": {"u0": "A"}},
        ))
        await s.commit()

        _panel_fakes(monkeypatch, enforcement)
        await enforcement.queue_restore(s, r, reason="test")
        res = await enforcement.process_enforcement_queue(s, action_limit=1)
        assert res["done"] == 1, res
        await s.refresh(r)
        assert r.enforcement_state == EnforcementState.active

        for inv in (old, fresh):
            await s.refresh(inv)
        # 40 days past its send date → genuinely past due.
        assert old.status == InvoiceStatus.overdue
        # 2 days old, warning_day = 5 → not past due yet; a blanket flip would have lied.
        assert fresh.status == InvoiceStatus.sent
        assert not (
            await s.execute(
                select(Invoice).where(Invoice.status == InvoiceStatus.enforced)
            )
        ).scalars().all()

    _run(body, tmp_path, "restore_clock.db")
