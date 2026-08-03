"""Bulk payment-deadline extension: POST /api/invoices/bulk-defer applies the same per-item
defer logic to several invoices, skipping (with a reason) any that can't be deferred."""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/bulkdefer.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.api import invoices as invoices_api  # noqa: E402
from app.models import DeliveryLog, Invoice, Panel, Reseller  # noqa: E402
from app.models.enums import DeliveryKind, DeliveryStatus, InvoiceStatus  # noqa: E402
from app.schemas.invoice import BulkDefer  # noqa: E402
from app.services.periods import today as tehran_today  # noqa: E402


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


async def _seed(s):
    s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o"))
    r = Reseller(panel_id=1, admin_uuid="a", name="R")
    s.add(r)
    await s.flush()
    return r


def _inv(reseller_id, *, label, status):
    y, m = (int(x) for x in label.split("-"))
    start = dt.date(y, m, 1)
    end = dt.date(y + (m // 12), (m % 12) + 1, 1) - dt.timedelta(days=1)
    return Invoice(reseller_id=reseller_id, panel_id=1, period_start=start, period_end=end,
                   period_label=label, usage_gb=10, amount_toman=1000, amount_usdt=1,
                   status=status, sent_at=dt.datetime.now(dt.timezone.utc))


def test_bulk_defer_applies_to_owed_and_skips_others(tmp_path):
    async def body(s):
        r = await _seed(s)
        owed1 = _inv(r.id, label="2026-01", status=InvoiceStatus.sent)
        owed2 = _inv(r.id, label="2026-02", status=InvoiceStatus.overdue)
        paid = _inv(r.id, label="2026-03", status=InvoiceStatus.paid)
        draft = _inv(r.id, label="2026-04", status=InvoiceStatus.draft)
        s.add_all([owed1, owed2, paid, draft])
        await s.flush()
        # A stale reminder on the overdue one — bulk defer must clear it (fresh cycle).
        s.add(DeliveryLog(reseller_id=r.id, invoice_id=owed2.id, kind=DeliveryKind.warning,
                          status=DeliveryStatus.sent))
        await s.commit()

        deadline = dt.date.today() + dt.timedelta(days=30)
        res = await invoices_api.bulk_defer(
            BulkDefer(ids=[owed1.id, owed2.id, paid.id, draft.id, 9999],
                      deferred_until=deadline, defer_note="مهلت گروهی"),
            session=s,
        )
        assert res.done == 2
        skipped_ids = {x["id"] for x in res.skipped}
        assert skipped_ids == {paid.id, draft.id, 9999}

        await s.refresh(owed1)
        await s.refresh(owed2)
        await s.refresh(paid)
        assert owed1.deferred_until == deadline and owed1.defer_note == "مهلت گروهی"
        assert owed2.deferred_until == deadline
        assert owed2.status == InvoiceStatus.sent          # overdue → sent on future deadline
        assert paid.deferred_until is None                 # untouched
        # The reminder log for owed2 was cleared (fresh dunning cycle).
        remaining = (
            await s.execute(
                DeliveryLog.__table__.select().where(DeliveryLog.invoice_id == owed2.id))
        ).all()
        assert remaining == []

    _run(body, tmp_path, "b1.db")


def test_bulk_defer_skips_dangling_invoice_instead_of_aborting(tmp_path):
    """N02 regression: an invoice whose panel row is gone must land in `skipped`, not
    abort the whole batch with a 409 (pre-N02 `_invoice_context` raised uncaught inside
    the loop and NOTHING was applied)."""
    async def body(s):
        r = await _seed(s)
        good1 = _inv(r.id, label="2026-01", status=InvoiceStatus.sent)
        good2 = _inv(r.id, label="2026-02", status=InvoiceStatus.overdue)
        s.add_all([good1, good2])
        await s.flush()
        # A second panel that then disappears — its invoice keeps the dangling panel_id
        # (constructible on SQLite tests, which don't enforce the FK; on Postgres the
        # panel FK has no CASCADE so app-level deletion order can leave the same state).
        s.add(Panel(id=2, key="p2", host="h2", proxy_path_enc="x", owner_uuid="o"))
        dangling = _inv(r.id, label="2026-03", status=InvoiceStatus.sent)
        dangling.panel_id = 2
        s.add(dangling)
        await s.flush()
        await s.execute(Panel.__table__.delete().where(Panel.__table__.c.id == 2))
        await s.commit()

        deadline = dt.date.today() + dt.timedelta(days=30)
        res = await invoices_api.bulk_defer(
            BulkDefer(ids=[good1.id, dangling.id, good2.id],
                      deferred_until=deadline, defer_note="مهلت گروهی"),
            session=s,
        )
        assert res.done == 2
        assert [x["id"] for x in res.skipped] == [dangling.id]
        assert "حذف" in res.skipped[0]["reason"]
        await s.refresh(good1)
        await s.refresh(good2)
        await s.refresh(dangling)
        assert good1.deferred_until == deadline and good2.deferred_until == deadline
        assert dangling.deferred_until is None             # untouched

    _run(body, tmp_path, "b3.db")


def test_bulk_defer_clear_deadline(tmp_path):
    async def body(s):
        r = await _seed(s)
        inv = _inv(r.id, label="2026-05", status=InvoiceStatus.sent)
        inv.deferred_until = dt.date.today() + dt.timedelta(days=10)
        s.add(inv)
        await s.commit()
        res = await invoices_api.bulk_defer(
            BulkDefer(ids=[inv.id], deferred_until=None, defer_note=None), session=s)
        assert res.done == 1
        await s.refresh(inv)
        assert inv.deferred_until is None

    _run(body, tmp_path, "b2.db")


def test_defer_to_today_resets_the_cycle(tmp_path):
    """«مهلت = امروز» must mean «as if issued today»: the reset guard is `>= today`, not `> today`.

    Under `>` a deadline of today only moved the dunning ANCHOR (dunning counts days from
    `deferred_until`) while the old reminder/warning marks survived. Since each reminder kind is
    sent at most once per invoice, the invoice then went silent for the whole new cycle and an
    already-overdue one kept its status.

    (Payability no longer enters into it at all — see
    `test_defer_only_moves_the_dunning_clock_not_the_right_to_pay`.)
    """
    async def body(s):
        r = await _seed(s)
        today = tehran_today()   # the calendar the code uses; date.today() flakes in a UTC CI
        inv = _inv(r.id, label="2026-06", status=InvoiceStatus.overdue)
        s.add(inv)
        await s.flush()
        for kind in (DeliveryKind.reminder1, DeliveryKind.reminder2, DeliveryKind.warning):
            s.add(DeliveryLog(reseller_id=r.id, invoice_id=inv.id, kind=kind,
                              status=DeliveryStatus.sent))
        await s.commit()

        res = await invoices_api.bulk_defer(
            BulkDefer(ids=[inv.id], deferred_until=today, defer_note=None), session=s)
        assert res.done == 1
        await s.refresh(inv)

        assert inv.deferred_until == today
        assert inv.status == InvoiceStatus.sent          # overdue → sent, a fresh cycle
        left = (await s.execute(
            __import__("sqlalchemy").select(DeliveryLog).where(DeliveryLog.invoice_id == inv.id)
        )).scalars().all()
        assert left == []                                 # stale reminders cleared → they re-fire
    _run(body, tmp_path, "b4.db")


def test_defer_only_moves_the_dunning_clock_not_the_right_to_pay(tmp_path):
    """A deadline is a promise not to CHASE the reseller yet — never a refusal to accept payment.

    It used to be both: every payment surface (bot pay flow, portal pay dialog, and the atomic
    guard in `payments.submit_payment`) skipped an invoice whose `deferred_until` was in the
    future, so granting someone extra time made their own debt unpayable until a date the OWNER
    picked. The reseller saw the pay button vanish.

    Now the deadline has exactly one effect: `dunning` re-anchors the reminder/warning/suspension
    cycle on it. This test pins BOTH halves so a future change can't fix one and break the other.
    """
    async def body(s):
        from app.services import enforcement

        r = await _seed(s)
        tomorrow = tehran_today() + dt.timedelta(days=1)
        inv = _inv(r.id, label="2026-07", status=InvoiceStatus.sent)
        s.add(inv)
        await s.commit()
        await invoices_api.bulk_defer(
            BulkDefer(ids=[inv.id], deferred_until=tomorrow, defer_note=None), session=s)
        await s.refresh(inv)

        assert inv.deferred_until == tomorrow
        # Half 1 — still owed, so every payability check (which is now just `status in OWED`)
        # lets it through.
        assert inv.status in (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)
        # Half 2 — the enforcement clock DOES respect the deadline: nothing is due yet, so a
        # queued suspension re-checking debt at execution time must find none.
        assert await enforcement._has_due_invoice(s, r.id) is False

    _run(body, tmp_path, "b5.db")
