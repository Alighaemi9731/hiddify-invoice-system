"""Cross-panel «پرداخت همهٔ بدهی» must restore EVERY reseller it settled, not just the first.

One Telegram account routinely owns reseller rows on several panels, and the payment flow is
explicitly built to settle their invoices across those panels in ONE transfer
(`submit_reseller_payment(reseller_ids=…)` validates each invoice against the whole SET).

The payment's own `reseller_id` is only the FIRST invoice's owner, and both confirm paths derived
the restore target from it — `targets[0]` on the automatic path, `all_in_set[0]` on the manual one.
So a suspended reseller on every OTHER panel stayed suspended even though the debt that suspended
them had just been paid, and nothing later corrected it: dunning only escalates, it never restores.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/payrestore.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import (  # noqa: E402
    EnforcementAction,
    Invoice,
    Panel,
    Payment,
    Reseller,
)
from app.models.enums import (  # noqa: E402
    EnforcementActionStatus,
    EnforcementActionType,
    EnforcementState,
    InvoiceStatus,
    PaymentMethod,
    PaymentStatus,
)
from app.services import payments  # noqa: E402

CHAT_ID = 555
HASH = "0x" + "ab" * 32


def _run(body, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()

    asyncio.run(go())


async def _seed_two_panels(s, *, extra_debt_on_b: bool = False):
    """ONE Telegram owner with a suspended reseller row on each of two panels, each owing one
    invoice. `extra_debt_on_b` gives panel B a SECOND unpaid invoice outside the payment set."""
    from app.services import settings_service

    await settings_service.set_value(s, "auto_restore_on_payment", True)
    out = []
    for idx, key in enumerate(("pa", "pb")):
        panel = Panel(key=key, host=f"{key}.invalid", proxy_path_enc="x", owner_uuid="o")
        s.add(panel)
        await s.flush()
        r = Reseller(panel_id=panel.id, admin_uuid=f"adm-{key}", name=f"R-{key}",
                     bot_chat_id=CHAT_ID, enforcement_state=EnforcementState.enforced,
                     max_users_snapshot=100, max_active_users_snapshot=100)
        s.add(r)
        await s.flush()
        inv = Invoice(
            reseller_id=r.id, panel_id=panel.id,
            period_start=dt.date(2026, 3, 1), period_end=dt.date(2026, 3, 31),
            period_label="2026-03", usage_gb=10, amount_toman=100_000, amount_usdt=1,
            status=InvoiceStatus.sent,
            sent_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=6),
        )
        s.add(inv)
        await s.flush()
        # The completed suspension this payment should now revert. `queue_restore` restores from
        # this exact source action, so without one there is nothing to queue.
        s.add(EnforcementAction(
            reseller_id=r.id, action=EnforcementActionType.disable_users, dry_run=False,
            affected_count=1, status=EnforcementActionStatus.done,
            snapshot={
                "limits": {r.admin_uuid: {"max_users": 100, "max_active_users": 100}},
                "admins": [r.admin_uuid],
                "users": {f"u-{key}": r.admin_uuid},
                "progress": {
                    "users_done": [f"u-{key}"], "admins_done": [r.admin_uuid],
                    "captured_limits": {
                        r.admin_uuid: {"max_users": 100, "max_active_users": 100}},
                },
            },
        ))
        out.append((r, inv))
        if extra_debt_on_b and idx == 1:
            s.add(Invoice(
                reseller_id=r.id, panel_id=panel.id,
                period_start=dt.date(2026, 4, 1), period_end=dt.date(2026, 4, 30),
                period_label="2026-04", usage_gb=5, amount_toman=50_000, amount_usdt=1,
                status=InvoiceStatus.sent,
                sent_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2),
            ))
    await s.commit()
    return out


async def _restore_targets(s) -> set[int]:
    rows = (await s.execute(
        select(EnforcementAction.reseller_id).where(
            EnforcementAction.action == EnforcementActionType.restore)
    )).scalars().all()
    return set(rows)


def test_manual_confirm_restores_every_reseller_in_the_set(tmp_path):
    async def body(s):
        (ra, ia), (rb, ib) = await _seed_two_panels(s)
        res = await payments.submit_reseller_payment(
            s, reseller_ids={ra.id, rb.id}, invoice_ids=[ia.id, ib.id], txid=HASH)
        assert res.status == "ok", res.user_message
        pid = res.payment.id
        # The payment belongs to the FIRST invoice's reseller — that is exactly the trap.
        assert res.payment.reseller_id == ra.id

        out = await payments.confirm_manually(s, pid)
        assert out.paid

        for inv_id in (ia.id, ib.id):
            assert (await s.get(Invoice, inv_id)).status == InvoiceStatus.paid

        assert await _restore_targets(s) == {ra.id, rb.id}, (
            "a reseller on another panel stayed suspended after their debt was paid"
        )

    _run(body, tmp_path, "manual.db")


def test_auto_verify_restores_every_reseller_in_the_set(tmp_path, monkeypatch):
    """Same invariant on the on-chain path, which derived its target from `targets[0]`."""
    async def body(s):
        from decimal import Decimal

        from app.services import settings_service

        (ra, ia), (rb, ib) = await _seed_two_panels(s)
        wallet = "0x" + "11" * 20
        contract = "0x" + "22" * 20
        await settings_service.set_value(s, "bscscan_api_key", "k")
        await settings_service.set_value(s, "usdt_bep20_address", wallet)
        await settings_service.set_value(s, "usdt_bep20_contract", contract)
        await settings_service.set_value(s, "min_confirmations", 1)
        await s.commit()

        res = await payments.submit_reseller_payment(
            s, reseller_ids={ra.id, rb.id}, invoice_ids=[ia.id, ib.id], txid=HASH)
        assert res.status == "ok"
        pid = res.payment.id

        async def fake_chain(api_url, api_key, w, c, txid):  # noqa: ANN001
            return payments._ChainCheck(
                found=True, to_address=wallet, from_address="0x" + "33" * 20,
                amount_usdt=Decimal("99"), confirmations=50, contract_address=contract,
            )

        monkeypatch.setattr(payments, "_bscscan_tokentx", fake_chain)

        out = await payments.verify_payment(s, pid, notify_reseller=False)
        assert out.paid, out.message_fa      # the AUTO path must genuinely have confirmed it

        assert await _restore_targets(s) == {ra.id, rb.id}

    _run(body, tmp_path, "auto.db")


def test_a_reseller_with_other_debt_is_not_restored(tmp_path):
    """Each reseller is judged on its OWN remaining debt: paying panel A must not release panel B
    while B still owes a different invoice."""
    async def body(s):
        (ra, ia), (rb, ib) = await _seed_two_panels(s, extra_debt_on_b=True)
        res = await payments.submit_reseller_payment(
            s, reseller_ids={ra.id, rb.id}, invoice_ids=[ia.id, ib.id], txid=HASH)
        assert res.status == "ok"
        await payments.confirm_manually(s, res.payment.id)

        targets = await _restore_targets(s)
        assert ra.id in targets, "the fully settled reseller must be restored"
        assert rb.id not in targets, "restored a reseller that still owes another invoice"

    _run(body, tmp_path, "otherdebt.db")


def test_reconfirming_does_not_duplicate_restores_or_notifications(tmp_path):
    async def body(s):
        (ra, ia), (rb, ib) = await _seed_two_panels(s)
        res = await payments.submit_reseller_payment(
            s, reseller_ids={ra.id, rb.id}, invoice_ids=[ia.id, ib.id], txid=HASH)
        pid = res.payment.id

        await payments.confirm_manually(s, pid)
        first = (await s.execute(
            select(EnforcementAction.id).where(
                EnforcementAction.action == EnforcementActionType.restore)
        )).scalars().all()

        await payments.confirm_manually(s, pid)      # owner double-click
        again = (await s.execute(
            select(EnforcementAction.id).where(
                EnforcementAction.action == EnforcementActionType.restore)
        )).scalars().all()

        assert sorted(first) == sorted(again), "re-confirmation created duplicate restore actions"
        pay = await s.get(Payment, pid)
        assert pay.status == PaymentStatus.confirmed
        assert pay.method in (PaymentMethod.usdt_txid, PaymentMethod.screenshot,
                              PaymentMethod.ton_txid, PaymentMethod.avax_txid)

    _run(body, tmp_path, "reconfirm.db")


def test_one_resellers_restore_failure_does_not_skip_the_others(tmp_path, monkeypatch):
    """A panel being unreachable must not strand the remaining resellers unevaluated."""
    async def body(s):
        (ra, ia), (rb, ib) = await _seed_two_panels(s)
        res = await payments.submit_reseller_payment(
            s, reseller_ids={ra.id, rb.id}, invoice_ids=[ia.id, ib.id], txid=HASH)

        from app.services import enforcement

        real = enforcement.queue_restore
        calls: list[int] = []

        async def flaky(session, reseller, **kw):
            calls.append(reseller.id)
            if reseller.id == ra.id:
                raise RuntimeError("panel unreachable")
            return await real(session, reseller, **kw)

        monkeypatch.setattr(enforcement, "queue_restore", flaky)
        await payments.confirm_manually(s, res.payment.id)

        assert set(calls) == {ra.id, rb.id}, "stopped evaluating after the first failure"
        assert rb.id in await _restore_targets(s)

    _run(body, tmp_path, "flaky.db")


# ── deleting one reseller must not silently destroy another's payment evidence ────────────────
async def _seed_cross_reseller_payment(s):
    """One customer, two panels, ONE transfer settling both panels' invoices — then confirmed."""
    from app.services import settings_service

    await settings_service.set_value(s, "auto_restore_on_payment", False)
    out = []
    for key in ("cx-a", "cx-b"):
        panel = Panel(key=key, host=f"{key}.invalid", proxy_path_enc="x", owner_uuid="o")
        s.add(panel)
        await s.flush()
        r = Reseller(panel_id=panel.id, admin_uuid=f"adm-{key}", name=f"R-{key}",
                     bot_chat_id=CHAT_ID)
        s.add(r)
        await s.flush()
        inv = Invoice(
            reseller_id=r.id, panel_id=panel.id,
            period_start=dt.date(2026, 3, 1), period_end=dt.date(2026, 3, 31),
            period_label="2026-03", usage_gb=10, amount_toman=100_000, amount_usdt=1,
            status=InvoiceStatus.sent,
            sent_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=6),
        )
        s.add(inv)
        await s.flush()
        out.append((panel, r, inv))
    await s.commit()

    (pa, ra, ia), (pb, rb, ib) = out
    res = await payments.submit_reseller_payment(
        s, reseller_ids={ra.id, rb.id}, invoice_ids=[ia.id, ib.id], txid=HASH)
    assert res.status == "ok", res.user_message
    await payments.confirm_manually(s, res.payment.id)
    return (pa, ra, ia), (pb, rb, ib), res.payment.id


def test_cross_reseller_payment_is_detected_before_a_delete(tmp_path):
    async def body(s):
        from app.services import payment_guard

        (_pa, ra, ia), (_pb, rb, ib), pay_id = await _seed_cross_reseller_payment(s)

        blocking = await payment_guard.blocking_payments(s, {ra.id})
        assert len(blocking) == 1, "the cross-reseller payment was not detected"
        assert blocking[0].payment_id == pay_id
        assert ib.id in blocking[0].foreign_invoice_ids
        assert rb.id in blocking[0].foreign_reseller_ids
        # …and the message names the invoice that would be stranded.
        assert str(ib.id) in payment_guard.refusal_message(blocking)

        # Deleting BOTH resellers together is fine — nothing is left stranded.
        assert await payment_guard.blocking_payments(s, {ra.id, rb.id}) == []
        # A reseller with no cross-payment is unaffected.
        assert await payment_guard.blocking_payments(s, set()) == []

    _run(body, tmp_path, "guard_detect.db")


def test_deleting_a_panel_with_a_cross_reseller_payment_is_refused(tmp_path):
    """The panel delete path was completely unguarded and destroyed payments via FK cascade."""
    from fastapi import HTTPException

    async def body(s):
        from app.api import panels as panels_api

        (pa, _ra, _ia), (_pb, _rb, ib), _pay = await _seed_cross_reseller_payment(s)

        try:
            await panels_api.delete_panel(pa.id, force=False, session=s)
        except HTTPException as exc:
            assert exc.status_code == 409
            assert str(ib.id) in exc.detail
        else:
            raise AssertionError("panel delete destroyed cross-panel payment evidence silently")

        assert await s.get(Panel, pa.id) is not None, "the panel was deleted despite the refusal"

    _run(body, tmp_path, "guard_panel.db")


def test_a_forced_panel_delete_archives_the_ledger_first(tmp_path):
    """Forcing is allowed — but the money facts must be preserved in the FK-free ledger before the
    evidence goes, so «تاریخچهٔ مالی» can still account for the surviving invoice."""
    async def body(s):
        from sqlalchemy import select as _select

        from app.api import panels as panels_api
        from app.models import FinancialRecord

        (pa, _ra, _ia), (_pb, _rb, ib), _pay = await _seed_cross_reseller_payment(s)

        await panels_api.delete_panel(pa.id, force=True, session=s)
        assert await s.get(Panel, pa.id) is None

        row = (await s.execute(
            _select(FinancialRecord).where(FinancialRecord.invoice_id == ib.id)
        )).scalar_one_or_none()
        assert row is not None, "the surviving invoice has no ledger row after a forced delete"
        assert row.txid == HASH, "the ledger did not capture the transaction hash"

    _run(body, tmp_path, "guard_force.db")


def test_deleting_an_invoice_prunes_it_from_settled_invoice_ids(tmp_path):
    """`payment_settlements` cascades on invoice_id but the comma column does not, so the two
    representations of one set silently diverged and `_settled_ids` kept returning a dead id."""
    async def body(s):
        from app.services import payment_guard

        (_pa, _ra, ia), (_pb, _rb, ib), pay_id = await _seed_cross_reseller_payment(s)

        before = await s.get(Payment, pay_id)
        assert str(ib.id) in (before.settled_invoice_ids or "")

        changed = await payment_guard.prune_deleted_invoice_ids(s, {ib.id})
        await s.commit()
        assert changed == 1

        after = await s.get(Payment, pay_id)
        await s.refresh(after)
        assert str(ib.id) not in (after.settled_invoice_ids or "")
        assert str(ia.id) in (after.settled_invoice_ids or "")
        assert after.invoice_id == ia.id, "the primary id still points at the deleted invoice"

    _run(body, tmp_path, "guard_prune.db")


def test_pruning_the_primary_invoice_repoints_it_at_a_survivor(tmp_path):
    async def body(s):
        from app.services import payment_guard

        (_pa, _ra, ia), (_pb, _rb, ib), pay_id = await _seed_cross_reseller_payment(s)
        payment = await s.get(Payment, pay_id)
        assert payment.invoice_id == ia.id          # the first invoice is the primary

        await payment_guard.prune_deleted_invoice_ids(s, {ia.id})
        await s.commit()

        after = await s.get(Payment, pay_id)
        await s.refresh(after)
        assert after.invoice_id == ib.id, "primary id was left dangling at a deleted invoice"
        assert (after.settled_invoice_ids or "") == str(ib.id)

    _run(body, tmp_path, "guard_prune_primary.db")
