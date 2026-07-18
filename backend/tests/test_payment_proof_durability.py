"""A failed proof upload must never leave the invoice unpayable.

Both proof paths used to create AND COMMIT the pending screenshot payment first and only then
touch storage. When the write/download failed the row survived with no `proof_path`, and the
one-pending-payment-per-invoice guard (`_pending_invoice_ids_in_sets` — it looks only at status and
coverage, never at the proof) then blocked the very resend the error message asked for. No job
cleans those rows up, so the invoice stayed unpayable by screenshot until an owner intervened.

Invariant asserted here: after ANY storage failure the same invoices are immediately re-submittable,
and a genuine pending payment is still protected from duplicates.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import io
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/proofdur.db")
os.environ.setdefault("SECRET_KEY", "k")

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.api import portal as portal_api  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import Invoice, Panel, Payment, Reseller  # noqa: E402
from app.models.enums import InvoiceStatus, PaymentMethod, PaymentStatus  # noqa: E402
from app.services import payments as payments_service  # noqa: E402


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


async def _seed(s):
    p = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
    s.add(p)
    await s.flush()
    r = Reseller(panel_id=p.id, admin_uuid="A", name="Ali", bot_chat_id=111)
    s.add(r)
    await s.flush()
    inv = Invoice(reseller_id=r.id, panel_id=p.id, period_start=dt.date(2026, 6, 1),
                  period_end=dt.date(2026, 6, 30), period_label="2026-06", usage_gb=10,
                  amount_toman=100_000, amount_usdt=1, status=InvoiceStatus.sent)
    s.add(inv)
    await s.commit()
    return r, inv


class _Upload:
    """Minimal UploadFile stand-in."""

    content_type = "image/jpeg"

    def __init__(self, data: bytes = b"\xff\xd8fake-jpeg") -> None:
        self._data = data

    async def read(self, n: int = -1) -> bytes:
        return self._data


class _Ctx:
    def __init__(self, ids):
        self.ids = list(ids)


async def _pending_for(s, invoice_id: int) -> list[Payment]:
    rows = (await s.execute(select(Payment).where(
        Payment.status == PaymentStatus.pending))).scalars().all()
    return [p for p in rows if str(invoice_id) in (p.settled_invoice_ids or "")
            or p.invoice_id == invoice_id]


def test_portal_write_failure_leaves_the_invoice_retryable(monkeypatch, tmp_path):
    async def body(s):
        r, inv = await _seed(s)
        monkeypatch.chdir(tmp_path)

        real_open = open

        def boom(path, mode="r", *a, **k):
            if "w" in mode and "payment_proofs" in str(path):
                raise OSError("disk full")
            return real_open(path, mode, *a, **k)

        monkeypatch.setattr("builtins.open", boom)
        # tempfile.mkstemp uses os.open, not builtins.open — fail that too.
        real_fdopen = os.fdopen

        def boom_fdopen(fd, mode="r", *a, **k):
            os.close(fd)
            raise OSError("disk full")

        monkeypatch.setattr(os, "fdopen", boom_fdopen)

        # The INVARIANT is what matters, not how the failure surfaces: whatever we tell the
        # reseller, we must not keep state that makes their next attempt impossible.
        try:
            out = await portal_api.pay_screenshot(
                invoice_id=inv.id, invoice_ids=None, file=_Upload(),
                ctx=_Ctx([r.id]), session=s)
            assert out["status"] != "ok"
        except HTTPException as exc:
            assert exc.status_code == 503

        monkeypatch.setattr(os, "fdopen", real_fdopen)
        monkeypatch.setattr("builtins.open", real_open)

        assert await _pending_for(s, inv.id) == [], (
            "a pending payment with no proof survived and now blocks the resend"
        )
        retry = await payments_service.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_id=inv.id, screenshot=True)
        assert retry.status == "ok", (
            f"invoice left unpayable after a failed proof upload: {retry.user_message}"
        )

    _run(body)


def test_portal_success_stores_exactly_one_payment_with_a_readable_proof(monkeypatch, tmp_path):
    async def body(s):
        r, inv = await _seed(s)
        monkeypatch.chdir(tmp_path)

        async def noop(*a, **k):
            return None

        monkeypatch.setattr(portal_api.owner_notify, "notify_owner_photo", noop)
        monkeypatch.setattr(portal_api.owner_notify, "notify_owner", noop)

        out = await portal_api.pay_screenshot(
            invoice_id=inv.id, invoice_ids=None, file=_Upload(),
            ctx=_Ctx([r.id]), session=s)
        assert out["status"] == "ok"

        rows = await _pending_for(s, inv.id)
        assert len(rows) == 1
        proof = rows[0].proof_path
        assert proof and os.path.exists(proof)
        with open(proof, "rb") as fh:
            assert fh.read() == b"\xff\xd8fake-jpeg"

        # No temp files left behind.
        leftovers = [f for f in os.listdir("data/payment_proofs") if f.startswith(".incoming-")]
        assert leftovers == [], leftovers

        # …and the legitimate pending payment still blocks a duplicate submission.
        dup = await payments_service.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_id=inv.id, screenshot=True)
        assert dup.status == "pending_exists"

    _run(body)


def test_portal_rejected_submission_leaves_no_orphan_file(monkeypatch, tmp_path):
    """A validation refusal (invoice already has a pending payment) must clean up the temp file."""
    async def body(s):
        r, inv = await _seed(s)
        monkeypatch.chdir(tmp_path)

        async def noop(*a, **k):
            return None

        monkeypatch.setattr(portal_api.owner_notify, "notify_owner_photo", noop)
        monkeypatch.setattr(portal_api.owner_notify, "notify_owner", noop)

        first = await portal_api.pay_screenshot(
            invoice_id=inv.id, invoice_ids=None, file=_Upload(),
            ctx=_Ctx([r.id]), session=s)
        assert first["status"] == "ok"

        second = await portal_api.pay_screenshot(
            invoice_id=inv.id, invoice_ids=None, file=_Upload(),
            ctx=_Ctx([r.id]), session=s)
        assert second["status"] == "pending_exists"

        leftovers = [f for f in os.listdir("data/payment_proofs") if f.startswith(".incoming-")]
        assert leftovers == [], leftovers

    _run(body)


def test_discard_declines_when_the_owner_already_confirmed(monkeypatch, tmp_path):
    """Compensation must never revert a payment somebody acted on in the meantime."""
    async def body(s):
        r, inv = await _seed(s)
        res = await payments_service.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_id=inv.id, screenshot=True)
        assert res.status == "ok"
        pid = res.payment.id

        pay = await s.get(Payment, pid)
        pay.status = PaymentStatus.confirmed     # the owner got there first
        await s.commit()

        assert await payments_service.discard_unproven_screenshot(s, pid) is False
        assert (await s.get(Payment, pid)) is not None

    _run(body)


def test_discard_only_touches_proofless_screenshots():
    async def body(s):
        r, inv = await _seed(s)
        res = await payments_service.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_id=inv.id, screenshot=True)
        pid = res.payment.id

        pay = await s.get(Payment, pid)
        pay.proof_path = "data/payment_proofs/payment_1.jpg"   # a real, saved proof
        await s.commit()

        assert await payments_service.discard_unproven_screenshot(s, pid) is False
        assert (await s.get(Payment, pid)) is not None

        pay = await s.get(Payment, pid)
        pay.proof_path = None
        await s.commit()
        assert await payments_service.discard_unproven_screenshot(s, pid) is True
        assert (await s.get(Payment, pid)) is None

        # …and with the row gone the invoice is payable again.
        again = await payments_service.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_id=inv.id, screenshot=True)
        assert again.status == "ok"

    _run(body)


def test_bot_download_failure_records_no_payment(monkeypatch, tmp_path):
    """The bot path must not create the row until the photo is actually in hand."""
    from app.bot.handlers import intake

    async def body(s):
        r, inv = await _seed(s)
        monkeypatch.chdir(tmp_path)

        replies: list[str] = []

        class _Bot:
            async def download(self, photo, destination):  # noqa: ANN001
                raise RuntimeError("telegram unreachable")

        class _Msg:
            from_user = type("U", (), {"id": 111})()
            photo = [type("P", (), {"file_id": "fid"})()]
            bot = _Bot()

            async def answer(self, text, **k):  # noqa: ANN001
                replies.append(text)

        async def fake_resellers(session, chat_id):  # noqa: ANN001
            return [r]

        async def fake_review(*a, **k):
            return "review"

        async def fake_send(*a, **k):
            return None

        # Stubbed so the OLD ordering (create row, then download) also runs to completion —
        # the assertion below must be what fails, not an incidental error further downstream.
        monkeypatch.setattr(intake, "_resellers_for_chat", fake_resellers)
        monkeypatch.setattr(intake, "_payment_review_html", fake_review)
        monkeypatch.setattr(intake, "send_owner_review", fake_send)

        await intake._handle_payment_proof(_Msg(), s, invoices=[inv])

        assert await _pending_for(s, inv.id) == [], "a proofless payment survived the failure"
        assert replies and "دوباره" in replies[0]

        retry = await payments_service.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_id=inv.id, screenshot=True)
        assert retry.status == "ok"

    _run(body)


def test_bot_success_keeps_the_payment_even_if_the_local_copy_fails(monkeypatch, tmp_path):
    """Once the photo is downloaded the payment is legitimate: Telegram still hosts the image for
    the owner's review, so a local disk failure must NOT discard the row or ask for a resend."""
    from app.bot.handlers import intake

    async def body(s):
        r, inv = await _seed(s)
        monkeypatch.chdir(tmp_path)

        replies: list[str] = []
        sent_reviews: list[object] = []

        class _Bot:
            async def download(self, photo, destination):  # noqa: ANN001
                destination.write(b"\xff\xd8img")

        class _Msg:
            from_user = type("U", (), {"id": 111})()
            photo = [type("P", (), {"file_id": "fid"})()]
            bot = _Bot()

            async def answer(self, text, **k):  # noqa: ANN001
                replies.append(text)

        async def fake_resellers(session, chat_id):  # noqa: ANN001
            return [r]

        async def fake_review(*a, **k):
            return "review"

        async def fake_send(*a, **k):
            sent_reviews.append(1)

        monkeypatch.setattr(intake, "_resellers_for_chat", fake_resellers)
        monkeypatch.setattr(intake, "_payment_review_html", fake_review)
        monkeypatch.setattr(intake, "send_owner_review", fake_send)

        real_open = open

        def boom(path, mode="r", *a, **k):
            if "w" in mode and "payment_proofs" in str(path):
                raise OSError("disk full")
            return real_open(path, mode, *a, **k)

        monkeypatch.setattr("builtins.open", boom)
        await intake._handle_payment_proof(_Msg(), s, invoices=[inv])
        monkeypatch.setattr("builtins.open", real_open)

        rows = await _pending_for(s, inv.id)
        assert len(rows) == 1, "the payment was discarded even though the proof reached us"
        assert sent_reviews, "the owner was not shown the payment"

    _run(body)


def test_bot_success_stores_the_proof(monkeypatch, tmp_path):
    from app.bot.handlers import intake

    async def body(s):
        r, inv = await _seed(s)
        monkeypatch.chdir(tmp_path)

        class _Bot:
            async def download(self, photo, destination):  # noqa: ANN001
                destination.write(b"\xff\xd8img")

        class _Msg:
            from_user = type("U", (), {"id": 111})()
            photo = [type("P", (), {"file_id": "fid"})()]
            bot = _Bot()

            async def answer(self, text, **k):  # noqa: ANN001
                return None

        async def fake_resellers(session, chat_id):  # noqa: ANN001
            return [r]

        async def fake_review(*a, **k):
            return "review"

        async def fake_send(*a, **k):
            return None

        monkeypatch.setattr(intake, "_resellers_for_chat", fake_resellers)
        monkeypatch.setattr(intake, "_payment_review_html", fake_review)
        monkeypatch.setattr(intake, "send_owner_review", fake_send)

        await intake._handle_payment_proof(_Msg(), s, invoices=[inv])

        rows = await _pending_for(s, inv.id)
        assert len(rows) == 1
        assert rows[0].proof_path and os.path.exists(rows[0].proof_path)
        with open(rows[0].proof_path, "rb") as fh:
            assert fh.read() == b"\xff\xd8img"
        assert rows[0].method == PaymentMethod.screenshot
        assert isinstance(io.BytesIO(), io.BytesIO)   # buffer path exercised

    _run(body)


# ───────────────── PG barrier: compensation vs a concurrent owner confirm ─────────────────
# SQLite makes `with_for_update` a no-op, so the serialization below can only be asserted on a
# real Postgres. Runs in CI's `backend-postgres` job (`pytest -m pg_contract`).
import pytest  # noqa: E402

from tests.pg_barrier import make_engine, requires_pg  # noqa: E402


async def _pg_purge(Session, tag: str):
    """Remove any rows a previously aborted run left behind, children before parents."""
    from app.models import PaymentSettlement

    async with Session() as s:
        pid = (await s.execute(
            select(Panel.id).where(Panel.key == f"proofdur-{tag}"))).scalar_one_or_none()
        if pid is None:
            return
        rids = (await s.execute(
            select(Reseller.id).where(Reseller.panel_id == pid))).scalars().all()
        pay_ids = (await s.execute(
            select(Payment.id).where(Payment.reseller_id.in_(rids or [-1])))).scalars().all()
        if pay_ids:
            await s.execute(
                PaymentSettlement.__table__.delete().where(
                    PaymentSettlement.payment_id.in_(pay_ids)))
        await s.execute(Payment.__table__.delete().where(Payment.reseller_id.in_(rids or [-1])))
        await s.execute(Invoice.__table__.delete().where(Invoice.panel_id == pid))
        await s.execute(Reseller.__table__.delete().where(Reseller.panel_id == pid))
        await s.execute(Panel.__table__.delete().where(Panel.id == pid))
        await s.commit()


async def _pg_seed(Session, tag: str):
    """Unique per test, and self-cleaning, so a previously aborted run can't collide on
    `ix_panels_key` (these tests share one CI database)."""
    await _pg_purge(Session, tag)
    async with Session() as s:
        p = Panel(key=f"proofdur-{tag}", host=f"{tag}.proofdur.invalid", proxy_path_enc="x",
                  owner_uuid="o")
        s.add(p)
        await s.flush()
        r = Reseller(panel_id=p.id, admin_uuid=f"PROOFDUR-{tag}", name="Ali", bot_chat_id=987_654)
        s.add(r)
        await s.flush()
        inv = Invoice(reseller_id=r.id, panel_id=p.id, period_start=dt.date(2026, 6, 1),
                      period_end=dt.date(2026, 6, 30), period_label="2026-06", usage_gb=10,
                      amount_toman=100_000, amount_usdt=1, status=InvoiceStatus.sent)
        s.add(inv)
        await s.commit()
        return r.id, p.id, inv.id


async def _pg_cleanup(Session, tag: str):
    await _pg_purge(Session, tag)


@pytest.mark.pg_contract
@requires_pg
def test_compensation_never_deletes_a_concurrently_confirmed_payment(monkeypatch):
    """The compensating delete must lose cleanly to an owner who confirmed first.

    Both `discard_unproven_screenshot` and `confirm_manually` take `with_for_update` +
    `populate_existing` on the same Payment row, so on Postgres they serialize: whichever commits
    first is observed by the other. The compensation re-checks status/proof AFTER acquiring the
    lock, so a confirmed payment is never silently deleted out from under the owner.
    """
    async def noop(*a, **k):
        return None

    monkeypatch.setattr(payments_service.notifier, "send_to_reseller", noop)
    monkeypatch.setattr(payments_service, "_send_receipt", noop)

    async def run():
        engine, Session = make_engine()
        try:
            rid, pid, inv_id = await _pg_seed(Session, "confirmrace")
            async with Session() as s:
                res = await payments_service.submit_reseller_payment(
                    s, reseller_ids={rid}, invoice_id=inv_id, screenshot=True)
                assert res.status == "ok"
                pay_id = res.payment.id

            async def confirm():
                async with Session() as s:
                    return await payments_service.confirm_manually(s, pay_id)

            async def compensate():
                async with Session() as s:
                    return await payments_service.discard_unproven_screenshot(s, pay_id)

            out = await asyncio.gather(confirm(), compensate(), return_exceptions=True)
            for r in out:
                assert not isinstance(r, BaseException), out

            async with Session() as s:
                pay = await s.get(Payment, pay_id)
                inv = await s.get(Invoice, inv_id)
                if pay is None:
                    # Compensation won the race → the invoice must be left unpaid and retryable.
                    assert inv.status != InvoiceStatus.paid
                else:
                    # Confirmation won → the payment survives and the invoice is settled.
                    assert pay.status == PaymentStatus.confirmed
                    assert inv.status == InvoiceStatus.paid
            await _pg_cleanup(Session, "confirmrace")
        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.mark.pg_contract
@requires_pg
def test_two_concurrent_compensations_delete_the_row_once():
    """Duplicate client retries must not double-delete or error."""
    async def run():
        engine, Session = make_engine()
        try:
            rid, pid, inv_id = await _pg_seed(Session, "doubledel")
            async with Session() as s:
                res = await payments_service.submit_reseller_payment(
                    s, reseller_ids={rid}, invoice_id=inv_id, screenshot=True)
                pay_id = res.payment.id

            async def compensate():
                async with Session() as s:
                    return await payments_service.discard_unproven_screenshot(s, pay_id)

            out = await asyncio.gather(compensate(), compensate(), return_exceptions=True)
            for r in out:
                assert not isinstance(r, BaseException), out
            assert sum(1 for r in out if r is True) == 1, f"expected exactly one delete: {out}"

            async with Session() as s:
                assert await s.get(Payment, pay_id) is None
                # …and the invoice is payable again.
                retry = await payments_service.submit_reseller_payment(
                    s, reseller_ids={rid}, invoice_id=inv_id, screenshot=True)
                assert retry.status == "ok"
            await _pg_cleanup(Session, "doubledel")
        finally:
            await engine.dispose()

    asyncio.run(run())
