"""A payment deadline must never take away the right to pay.

Reported by the owner: after granting an invoice a «مهلت پرداخت», the reseller could no longer pay
it — the pay button disappeared from the bot and the portal, and a stale button answered «این
فاکتور در حال حاضر قابل پرداخت نیست». That is backwards. A deadline is a promise not to CHASE them
yet; it is not a refusal to accept their money.

`deferred_until` now has exactly ONE effect: `dunning` re-anchors the reminder / warning /
suspension cycle on it. Every payment and debt-display path ignores it.

The two halves are tested together on purpose — the failure mode to guard against is fixing one
and silently breaking the other (making a deferred invoice payable AND immediately chaseable would
defeat the whole point of granting the deadline).
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/defpay.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import Invoice, Panel, Reseller  # noqa: E402
from app.models.enums import InvoiceStatus  # noqa: E402
from app.services import payments  # noqa: E402
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


async def _seed(s, *, deferred_until=None, status=InvoiceStatus.sent):
    s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o"))
    r = Reseller(panel_id=1, admin_uuid="a", name="R")
    s.add(r)
    await s.flush()
    inv = Invoice(
        reseller_id=r.id, panel_id=1, period_label="2026-07",
        period_start=dt.date(2026, 7, 1), period_end=dt.date(2026, 7, 31),
        usage_gb=10, price_per_gb=1000, amount_toman=10000, status=status,
        sent_at=dt.datetime.now(dt.timezone.utc), deferred_until=deferred_until,
    )
    s.add(inv)
    await s.commit()
    return r, inv


def test_a_future_deadline_does_not_block_submitting_a_payment(tmp_path):
    """THE bug: `submit_reseller_payment`'s atomic re-validation rejected the whole batch when any
    chosen invoice had a future `deferred_until`, so the reseller's proof bounced with
    «قابل پرداخت نیست»."""
    async def body(s):
        tomorrow = tehran_today() + dt.timedelta(days=1)
        r, inv = await _seed(s, deferred_until=tomorrow)

        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid="0x" + "a1" * 32)

        assert res.status == "ok", f"a deferred invoice was refused: {res.user_message}"
        assert res.payment is not None
        assert res.payment.settled_invoice_ids == str(inv.id)   # stored comma-joined

    _run(body, tmp_path, "d1.db")


def test_a_deferred_invoice_can_ride_along_in_a_pay_all_batch(tmp_path):
    """«پرداخت همهٔ بدهی» sums every owed invoice including deferred ones (owner's decision), so
    the atomic guard must not reject the batch because of the deferred member."""
    async def body(s):
        tomorrow = tehran_today() + dt.timedelta(days=1)
        r, due = await _seed(s)
        deferred = Invoice(
            reseller_id=r.id, panel_id=1, period_label="2026-08",
            period_start=dt.date(2026, 8, 1), period_end=dt.date(2026, 8, 31),
            usage_gb=5, price_per_gb=1000, amount_toman=5000, status=InvoiceStatus.sent,
            sent_at=dt.datetime.now(dt.timezone.utc), deferred_until=tomorrow,
        )
        s.add(deferred)
        await s.commit()

        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[due.id, deferred.id], txid="0x" + "b2" * 32)

        assert res.status == "ok", f"the batch was refused: {res.user_message}"
        assert {int(x) for x in res.payment.settled_invoice_ids.split(",")} == {due.id, deferred.id}
        assert float(res.payment.amount_toman) == 15000.0   # the SUM of both

    _run(body, tmp_path, "d2.db")


def test_the_enforcement_half_still_respects_the_deadline(tmp_path):
    """The other half, and the reason the deadline exists at all: nothing may chase or suspend the
    reseller before it. `_has_due_invoice` is the re-check every queued suspension runs at
    execution time, so it is the load-bearing one."""
    async def body(s):
        from app.services import enforcement

        tomorrow = tehran_today() + dt.timedelta(days=1)
        r, _inv = await _seed(s, deferred_until=tomorrow)

        assert await enforcement._has_due_invoice(s, r.id) is False

        # …and the day the deadline lands, the reseller is due again.
        _inv.deferred_until = tehran_today()
        await s.commit()
        assert await enforcement._has_due_invoice(s, r.id) is True

    _run(body, tmp_path, "d3.db")


def test_paying_a_deferred_invoice_does_not_unsuspend_someone_who_still_owes(tmp_path):
    """Making deferred invoices payable must not weaken the restore guard: `_reseller_has_other_due`
    still treats a FUTURE deadline as 'not currently due', which is what lets a granted deadline
    lift a suspension — while a genuinely due invoice elsewhere keeps it in place."""
    async def body(s):
        tomorrow = tehran_today() + dt.timedelta(days=1)
        r, paid_one = await _seed(s)
        other_due = Invoice(
            reseller_id=r.id, panel_id=1, period_label="2026-05",
            period_start=dt.date(2026, 5, 1), period_end=dt.date(2026, 5, 31),
            usage_gb=5, price_per_gb=1000, amount_toman=5000, status=InvoiceStatus.sent,
            sent_at=dt.datetime.now(dt.timezone.utc),
        )
        s.add(other_due)
        await s.commit()

        # Another invoice is due now → paying this one must NOT restore service.
        assert await payments._reseller_has_other_due(s, r.id, {paid_one.id}) is True

        # Give that other one a deadline too → nothing is currently due → restore is allowed.
        other_due.deferred_until = tomorrow
        await s.commit()
        assert await payments._reseller_has_other_due(s, r.id, {paid_one.id}) is False

    _run(body, tmp_path, "d4.db")
