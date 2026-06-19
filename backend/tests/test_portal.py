"""Reseller web-portal: Telegram one-time-login → reseller session, strict per-reseller scoping,
and owner/reseller role isolation."""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/portal.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.api import portal  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.core.portal_auth import (  # noqa: E402
    create_portal_login_token,
    create_portal_session_token,
    get_current_reseller,
    verify_portal_login_token,
)
from app.core.security import create_access_token, get_current_subject  # noqa: E402
from app.models import Invoice, Panel, Payment, Reseller  # noqa: E402
from app.models.enums import InvoiceStatus, PaymentMethod, PaymentStatus  # noqa: E402


def _run(body):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


async def _seed(s, with_payments: bool = True):
    p = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
    s.add(p)
    await s.flush()
    a = Reseller(panel_id=p.id, admin_uuid="A", name="Ali", bot_chat_id=111)
    b = Reseller(panel_id=p.id, admin_uuid="B", name="Bita", bot_chat_id=222)
    s.add_all([a, b])
    await s.flush()
    inv_a = Invoice(reseller_id=a.id, panel_id=p.id, period_start=dt.date(2026, 6, 1),
                    period_end=dt.date(2026, 6, 30), period_label="2026-06", usage_gb=10,
                    amount_toman=100_000, status=InvoiceStatus.sent)
    inv_b = Invoice(reseller_id=b.id, panel_id=p.id, period_start=dt.date(2026, 6, 1),
                    period_end=dt.date(2026, 6, 30), period_label="2026-06", usage_gb=5,
                    amount_toman=50_000, status=InvoiceStatus.sent)
    s.add_all([inv_a, inv_b])
    await s.flush()
    if with_payments:
        s.add_all([
            Payment(reseller_id=a.id, invoice_id=inv_a.id, method=PaymentMethod.ton_txid,
                    chain="ton", status=PaymentStatus.pending, txid="aaa"),
            Payment(reseller_id=b.id, invoice_id=inv_b.id, method=PaymentMethod.ton_txid,
                    chain="ton", status=PaymentStatus.pending, txid="bbb"),
        ])
    await s.commit()
    return a, b, inv_a, inv_b


def test_login_token_roundtrip_and_expiry():
    assert verify_portal_login_token(create_portal_login_token(111)) == 111
    assert verify_portal_login_token("garbage") is None
    # a session token is NOT a login token (wrong typ)
    assert verify_portal_login_token(create_portal_session_token(111)) is None


def test_get_current_reseller_scopes_to_chat_id():
    async def body(s):
        a, _b, _ia, _ib = await _seed(s)
        ctx = await get_current_reseller(create_portal_session_token(111), s)
        assert ctx.chat_id == 111 and ctx.ids == [a.id]
        # unknown telegram id → rejected
        with pytest.raises(HTTPException) as ei:
            await get_current_reseller(create_portal_session_token(999), s)
        assert ei.value.status_code == 401
    _run(body)


def test_role_isolation():
    async def body(s):
        await _seed(s)
        owner_tok = create_access_token("admin", extra={"role": "owner", "epoch": 0})
        # owner token cannot pass the reseller dependency
        with pytest.raises(HTTPException):
            await get_current_reseller(owner_tok, s)
        # reseller token cannot pass the owner dependency (role != owner, rejected pre-DB)
        with pytest.raises(HTTPException):
            await get_current_subject(create_portal_session_token(111), s)
    _run(body)


def test_exchange_endpoint():
    async def body(s):
        await _seed(s)
        ok = await portal.exchange(portal.ExchangeBody(token=create_portal_login_token(111)), s)
        assert ok["access_token"] and ok["token_type"] == "bearer"
        with pytest.raises(HTTPException) as bad:
            await portal.exchange(portal.ExchangeBody(token="nope"), s)
        assert bad.value.status_code == 401
        with pytest.raises(HTTPException) as noreseller:
            await portal.exchange(portal.ExchangeBody(token=create_portal_login_token(999)), s)
        assert noreseller.value.status_code == 403
    _run(body)


def test_invoices_payments_scoped_and_pdf_ownership():
    async def body(s):
        a, _b, _ia, inv_b = await _seed(s)
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)

        inv_rows = await portal.invoices(ctx=ctx_a, session=s)
        assert len(inv_rows) == 1 and inv_rows[0]["amount_toman"] == 100_000

        pay_rows = await portal.list_payments(ctx=ctx_a, session=s)
        assert len(pay_rows) == 1 and pay_rows[0]["txid"] == "aaa"

        # Ali cannot fetch Bita's invoice PDF (404 before any rendering)
        with pytest.raises(HTTPException) as ei:
            await portal.invoice_pdf(invoice_id=inv_b.id, ctx=ctx_a, session=s)
        assert ei.value.status_code == 404
    _run(body)


def test_summary_shape_and_trend_length():
    async def body(s):
        await _seed(s)
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)
        out = await portal.summary(period="2026-06", ctx=ctx_a, session=s)
        assert out["period"] == "2026-06"
        assert len(out["trend"]) == 30
        assert set(out["estimate"]) == {"amount_toman", "gb", "users"}
        assert out["outstanding"]["count"] == 1  # Ali's one owed invoice
    _run(body)


def test_summary_rejects_malformed_period():
    async def body(s):
        await _seed(s)
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)
        with pytest.raises(HTTPException) as ei:
            await portal.summary(period="2026-13", ctx=ctx_a, session=s)
        assert ei.value.status_code == 400
    _run(body)


# ============================ P2: actions ============================
from app.services import payments as payments_service  # noqa: E402


def test_submit_payment_core_ok_and_one_pending():
    async def body(s):
        a, _b, inv_a, _ib = await _seed(s, with_payments=False)
        r1 = await payments_service.submit_reseller_payment(
            s, reseller_ids={a.id}, invoice_id=inv_a.id, txid="newtx", chain="bsc")
        assert r1.status == "ok" and r1.payment is not None and r1.notify
        assert r1.payment.invoice_id == inv_a.id and r1.payment.reseller_id == a.id
        # second submission for the SAME invoice is blocked (one pending per invoice)
        r2 = await payments_service.submit_reseller_payment(
            s, reseller_ids={a.id}, invoice_id=inv_a.id, txid="othertx", chain="bsc")
        assert r2.status == "pending_exists" and r2.payment is None
    _run(body)


def test_submit_payment_core_duplicate_and_reopen():
    async def body(s):
        a, b, inv_a, inv_b = await _seed(s)
        from app.models.enums import PaymentStatus
        # the seeded pending TON payment for Ali used txid "aaa"
        dup = await payments_service.submit_reseller_payment(
            s, reseller_ids={a.id}, invoice_id=inv_a.id, txid="aaa", chain="ton")
        assert dup.status == "dup_pending"
        # Bita's payment "bbb" → mark rejected, then Ali cannot re-open it (not his)
        pay_b = (await s.execute(select(Payment).where(Payment.txid == "bbb"))).scalar_one()
        pay_b.status = PaymentStatus.rejected
        await s.commit()
        wrong = await payments_service.submit_reseller_payment(
            s, reseller_ids={a.id}, invoice_id=inv_a.id, txid="bbb", chain="ton")
        assert wrong.status == "wrong_owner"
        # Bita re-opens her own rejected txid
        reopen = await payments_service.submit_reseller_payment(
            s, reseller_ids={b.id}, invoice_id=inv_b.id, txid="bbb", chain="ton")
        assert reopen.status == "reopened" and reopen.payment.id == pay_b.id
    _run(body)


def test_submit_payment_core_not_payable():
    async def body(s):
        a, _b, inv_a, inv_b = await _seed(s, with_payments=False)
        from app.models.enums import InvoiceStatus as _IS
        # Cannot pay another reseller's invoice
        cross = await payments_service.submit_reseller_payment(
            s, reseller_ids={a.id}, invoice_id=inv_b.id, txid="x1", chain="bsc")
        assert cross.status == "not_payable"
        # Cannot pay a paid invoice
        inv_a.status = _IS.paid
        await s.commit()
        paid = await payments_service.submit_reseller_payment(
            s, reseller_ids={a.id}, invoice_id=inv_a.id, txid="x2", chain="bsc")
        assert paid.status == "not_payable"
    _run(body)


def test_submit_payment_core_screenshot():
    async def body(s):
        a, _b, inv_a, _ib = await _seed(s, with_payments=False)
        from app.models.enums import PaymentMethod
        r = await payments_service.submit_reseller_payment(
            s, reseller_ids={a.id}, invoice_id=inv_a.id, screenshot=True)
        assert r.status == "ok" and r.payment.method == PaymentMethod.screenshot
        assert "رسید" in r.user_message
    _run(body)


async def _noop_notify(*a, **k):
    return None


def test_pay_options_ownership_and_pay_txid(monkeypatch):
    monkeypatch.setattr(portal, "_notify_owner_new_payment", _noop_notify)

    async def body(s):
        a, _b, inv_a, inv_b = await _seed(s, with_payments=False)
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)
        # cannot read another reseller's invoice options
        with pytest.raises(HTTPException) as ei:
            await portal.pay_options(invoice_id=inv_b.id, ctx=ctx_a, session=s)
        assert ei.value.status_code == 404
        opt = await portal.pay_options(invoice_id=inv_a.id, ctx=ctx_a, session=s)
        assert opt["payable"] is True and opt["invoice"]["amount_toman"] == 100_000

        # empty txid → 400
        with pytest.raises(HTTPException) as bad:
            await portal.pay_txid(
                portal.PayTxidBody(invoice_id=inv_a.id, txid="  "), ctx=ctx_a, session=s)
        assert bad.value.status_code == 400

        out = await portal.pay_txid(
            portal.PayTxidBody(invoice_id=inv_a.id, txid="portaltx", chain="bsc"),
            ctx=ctx_a, session=s)
        assert out["status"] == "ok" and out["number"]
        created = (await s.execute(select(Payment).where(Payment.txid == "portaltx"))).scalar_one()
        assert created.reseller_id == a.id and created.invoice_id == inv_a.id
    _run(body)


async def _seed_with_sub(s):
    """Ali (chat 111) is a top-level reseller with one sub-reseller 'Sara'; Bita owns 'Other'."""
    p = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
    s.add(p)
    await s.flush()
    ali = Reseller(panel_id=p.id, admin_uuid="A", name="Ali", bot_chat_id=111, panel_max_users=500)
    bita = Reseller(panel_id=p.id, admin_uuid="B", name="Bita", bot_chat_id=222)
    s.add_all([ali, bita])
    await s.flush()
    sara = Reseller(panel_id=p.id, admin_uuid="S", name="Sara", parent_admin_uuid="A",
                    panel_max_users=100, can_add_admin=False)
    other = Reseller(panel_id=p.id, admin_uuid="O", name="Other", parent_admin_uuid="B")
    s.add_all([sara, other])
    await s.commit()
    return ali, sara, other


def test_sub_cap_and_ownership():
    async def body(s):
        _ali, sara, other = await _seed_with_sub(s)
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)
        # set a cap on the caller's own sub
        res = await portal.sub_cap(sub_id=sara.id, body=portal.CapBody(gb=500), ctx=ctx_a, session=s)
        assert res["ok"] and res["gb_cap"] == 500
        refreshed = await s.get(Reseller, sara.id)
        assert refreshed.gb_cap == 500
        # 0 clears the cap
        res0 = await portal.sub_cap(sub_id=sara.id, body=portal.CapBody(gb=0), ctx=ctx_a, session=s)
        assert res0["gb_cap"] is None
        # cannot touch a sub that isn't in the caller's subtree
        with pytest.raises(HTTPException) as ei:
            await portal.sub_cap(sub_id=other.id, body=portal.CapBody(gb=10), ctx=ctx_a, session=s)
        assert ei.value.status_code == 404
    _run(body)


def test_sub_restore_not_enforced_and_suspend_ownership():
    async def body(s):
        _ali, sara, other = await _seed_with_sub(s)
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)
        # restoring a non-suspended sub is a safe no-op
        out = await portal.sub_restore(sub_id=sara.id, ctx=ctx_a, session=s)
        assert out["status"] == "not_enforced"
        # cannot suspend someone else's sub
        with pytest.raises(HTTPException) as ei:
            await portal.sub_suspend(sub_id=other.id, ctx=ctx_a, session=s)
        assert ei.value.status_code == 404
    _run(body)


def test_support_validation_and_relay(monkeypatch):
    async def body(s):
        await _seed(s)
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)
        # empty → 400
        with pytest.raises(HTTPException) as bad:
            await portal.support(portal.SupportBody(text="  "), ctx=ctx_a, session=s)
        assert bad.value.status_code == 400
        # no owner chat / bot configured → notify fails → 503
        with pytest.raises(HTTPException) as down:
            await portal.support(portal.SupportBody(text="سلام"), ctx=ctx_a, session=s)
        assert down.value.status_code == 503

        # with notify_owner patched to succeed → ok
        async def _ok(*a, **k):
            return True
        monkeypatch.setattr("app.services.owner_notify.notify_owner", _ok)
        out = await portal.support(portal.SupportBody(text="سلام"), ctx=ctx_a, session=s)
        assert out["ok"] is True
    _run(body)


# ===================== feature batch v2 =====================
def test_subs_response_has_capacity_and_months():
    async def body(s):
        await _seed_with_sub(s)
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)
        rows = await portal.subs(ctx=ctx_a, session=s)
        assert len(rows) == 1  # only Ali's direct sub (Sara), not Bita's
        sara = rows[0]
        assert sara["max_users"] == 100
        assert sara["can_add_admin"] is False
        assert isinstance(sara["months"], list) and len(sara["months"]) == 6
        assert {"label", "amount_toman", "gb"} <= set(sara["months"][0])
    _run(body)


def test_sub_pdf_ownership_and_no_sales(monkeypatch):
    async def _none(*a, **k):
        return None
    monkeypatch.setattr("app.services.invoice_pdf.render_sub_invoice_pdf", _none)

    async def body(s):
        _ali, sara, other = await _seed_with_sub(s)
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)
        # foreign sub → 404 (before any render)
        with pytest.raises(HTTPException) as ei:
            await portal.sub_pdf(sub_id=other.id, period="2026-06", ctx=ctx_a, session=s)
        assert ei.value.status_code == 404
        # own sub but no sales (render→None) → 404
        with pytest.raises(HTTPException) as no_sales:
            await portal.sub_pdf(sub_id=sara.id, period="2026-06", ctx=ctx_a, session=s)
        assert no_sales.value.status_code == 404
    _run(body)


def test_sub_bump_limits(monkeypatch):
    calls = {}

    async def _bump(session, reseller, amount):
        calls["amount"] = amount
        return (100 + amount, 100 + amount)
    monkeypatch.setattr("app.services.admin_capacity.bump_limits", _bump)

    async def body(s):
        _ali, sara, other = await _seed_with_sub(s)
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)
        # invalid amounts → 400
        for amt in (0, -5, 6000):
            with pytest.raises(HTTPException) as bad:
                await portal.sub_bump_limits(sub_id=sara.id, body=portal.BumpBody(amount=amt), ctx=ctx_a, session=s)
            assert bad.value.status_code == 400
        # foreign sub → 404
        with pytest.raises(HTTPException) as ei:
            await portal.sub_bump_limits(sub_id=other.id, body=portal.BumpBody(amount=100), ctx=ctx_a, session=s)
        assert ei.value.status_code == 404
        # valid → calls the shared service
        out = await portal.sub_bump_limits(sub_id=sara.id, body=portal.BumpBody(amount=100), ctx=ctx_a, session=s)
        assert out["max_users"] == 200 and calls["amount"] == 100
    _run(body)


def test_sub_can_add_admin(monkeypatch):
    async def _set(session, reseller, enabled):
        reseller.can_add_admin = enabled
    monkeypatch.setattr("app.services.admin_capacity.set_can_add_admin", _set)

    async def body(s):
        _ali, sara, other = await _seed_with_sub(s)
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)
        with pytest.raises(HTTPException) as ei:
            await portal.sub_can_add_admin(sub_id=other.id, body=portal.CanAddAdminBody(enabled=True), ctx=ctx_a, session=s)
        assert ei.value.status_code == 404
        out = await portal.sub_can_add_admin(sub_id=sara.id, body=portal.CanAddAdminBody(enabled=True), ctx=ctx_a, session=s)
        assert out["ok"] and out["can_add_admin"] is True
    _run(body)


def test_capacity_and_request(monkeypatch):
    async def body(s):
        ali, _sara, _other = await _seed_with_sub(s)
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)
        cap = await portal.capacity(ctx=ctx_a, session=s)
        assert len(cap) == 1 and cap[0]["reseller_id"] == ali.id
        assert cap[0]["max"] == 500 and cap[0]["used"] == 0

        # request for a reseller not owned by the caller → 404
        with pytest.raises(HTTPException) as ei:
            await portal.capacity_request(
                portal.CapacityRequestBody(reseller_id=99999, amount=100), ctx=ctx_a, session=s)
        assert ei.value.status_code == 404

        # owner unreachable (no bot) → 503
        with pytest.raises(HTTPException) as down:
            await portal.capacity_request(
                portal.CapacityRequestBody(reseller_id=ali.id, amount=100), ctx=ctx_a, session=s)
        assert down.value.status_code == 503

        async def _ok(*a, **k):
            return True
        monkeypatch.setattr("app.services.owner_notify.notify_owner", _ok)
        out = await portal.capacity_request(
            portal.CapacityRequestBody(reseller_id=ali.id, amount=100, note="نیاز دارم"),
            ctx=ctx_a, session=s)
        assert out["ok"] is True
    _run(body)


def test_payment_proof_ownership():
    async def body(s):
        a, _b, _ia, _ib = await _seed(s)  # seeds a pending payment per reseller, no proof file
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)
        own = (await s.execute(select(Payment).where(Payment.reseller_id == a.id))).scalar_one()
        bita_pay = (await s.execute(select(Payment).where(Payment.txid == "bbb"))).scalar_one()
        # own payment but no proof image on disk → 404
        with pytest.raises(HTTPException) as no_img:
            await portal.payment_proof(payment_id=own.id, ctx=ctx_a, session=s)
        assert no_img.value.status_code == 404
        # another reseller's payment → 404 (ownership)
        with pytest.raises(HTTPException) as foreign:
            await portal.payment_proof(payment_id=bita_pay.id, ctx=ctx_a, session=s)
        assert foreign.value.status_code == 404
    _run(body)


def test_notifications_scoped_and_sorted():
    async def body(s):
        await _seed(s)  # Ali: one sent invoice + one pending payment
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)
        out = await portal.notifications(ctx=ctx_a, session=s)
        evs = out["events"]
        assert len(evs) >= 2 and len(evs) <= 20
        types = {e["type"] for e in evs}
        assert "invoice" in types and "payment" in types
        # none of Bita's events leak in (only keys for Ali's invoice/payment ids)
        keys = " ".join(e["key"] for e in evs)
        assert "bbb" not in keys  # Bita's txid never surfaces
    _run(body)


# ===================== follow-ups: daily trend + capacity approve =====================
def test_sub_sales_by_day_ownership_and_shape():
    async def body(s):
        _ali, sara, other = await _seed_with_sub(s)
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)
        rows = await portal.sub_sales_by_day(sub_id=sara.id, period="2026-06", ctx=ctx_a, session=s)
        assert len(rows) == 30  # June has 30 days
        assert {"day", "date", "amount_toman"} <= set(rows[0])
        assert rows[0]["day"] == 1 and rows[-1]["day"] == 30
        # foreign sub → 404
        with pytest.raises(HTTPException) as ei:
            await portal.sub_sales_by_day(sub_id=other.id, period="2026-06", ctx=ctx_a, session=s)
        assert ei.value.status_code == 404
        # bad period → 400
        with pytest.raises(HTTPException) as bad:
            await portal.sub_sales_by_day(sub_id=sara.id, period="2026-13", ctx=ctx_a, session=s)
        assert bad.value.status_code == 400
    _run(body)


def test_capacity_request_attaches_action_keyboard(monkeypatch):
    captured = {}

    async def _capture(session, text, *, html=False, reply_markup=None):
        captured["markup"] = reply_markup
        return True
    monkeypatch.setattr("app.services.owner_notify.notify_owner", _capture)

    async def body(s):
        ali, _sara, _other = await _seed_with_sub(s)
        ctx_a = await get_current_reseller(create_portal_session_token(111), s)
        await portal.capacity_request(
            portal.CapacityRequestBody(reseller_id=ali.id, amount=150), ctx=ctx_a, session=s)
        data = [b.callback_data for row in captured["markup"].inline_keyboard for b in row]
        assert any(d == f"capok:{ali.id}:150" for d in data)   # approve the requested amount
        assert any(d == f"capmore:{ali.id}" for d in data)     # custom amount
        assert any(d == f"capno:{ali.id}" for d in data)       # reject
    _run(body)


def test_capacity_request_keyboard_variants():
    from app.bot import keyboards as kb
    with_amount = [b.callback_data for row in kb.capacity_request_keyboard(7, 200).inline_keyboard for b in row]
    assert "capok:7:200" in with_amount and "capmore:7" in with_amount and "capno:7" in with_amount
    no_amount = [b.callback_data for row in kb.capacity_request_keyboard(7, 0).inline_keyboard for b in row]
    assert not any(d.startswith("capok:") for d in no_amount)  # nothing to approve without an amount
    assert "capmore:7" in no_amount and "capno:7" in no_amount
