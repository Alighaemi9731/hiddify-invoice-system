"""P05: owner-side disk/PII hygiene — expired portal nonces, terminal-payment proof files,
cached invoice PDFs, and tire-kicker bot_users are pruned; DB money rows + registered users kept."""
from __future__ import annotations

import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/ownerdata.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import (  # noqa: E402
    BotUser,
    Panel,
    Payment,
    PortalLoginNonce,
    Reseller,
)
from app.models.enums import PaymentMethod, PaymentStatus  # noqa: E402
from app.services import maintenance, settings_service  # noqa: E402

NOW = dt.datetime(2026, 7, 4, 12, 0, tzinfo=dt.timezone.utc)
OLD = NOW - dt.timedelta(days=400)


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


def test_expired_nonces_pruned_unexpired_kept(tmp_path):
    async def body(s):
        s.add(PortalLoginNonce(jti="dead", expires_at=NOW - dt.timedelta(hours=1)))
        s.add(PortalLoginNonce(jti="live", expires_at=NOW + dt.timedelta(hours=1)))
        await s.commit()
        c = await maintenance.prune_owner_data(s, now=NOW)
        assert c["nonces"] == 1
        left = (await s.execute(select(PortalLoginNonce.jti))).scalars().all()
        assert left == ["live"]
    _run(body, tmp_path, "d1.db")


def test_terminal_payment_proof_file_removed_row_kept(tmp_path):
    async def body(s):
        panel = Panel(key="p", host="h", proxy_path_enc="x", owner_uuid="o")
        s.add(panel)
        await s.flush()
        r = Reseller(panel_id=panel.id, admin_uuid="a", name="R")
        s.add(r)
        await s.flush()
        proof = tmp_path / "proof_conf.jpg"
        proof.write_bytes(b"x")
        pending_proof = tmp_path / "proof_pending.jpg"
        pending_proof.write_bytes(b"y")
        conf = Payment(reseller_id=r.id, method=PaymentMethod.screenshot,
                       status=PaymentStatus.confirmed, proof_path=str(proof),
                       verified_at=OLD, amount_usdt=0)
        pend = Payment(reseller_id=r.id, method=PaymentMethod.screenshot,
                       status=PaymentStatus.pending, proof_path=str(pending_proof), amount_usdt=0)
        conf.created_at = OLD  # type: ignore[attr-defined]
        s.add_all([conf, pend])
        await s.commit()
        c = await maintenance.prune_owner_data(s, now=NOW)
        assert c["proof_files"] == 1
        assert not proof.exists()          # terminal payment's file removed
        assert pending_proof.exists()      # pending payment's proof kept (still under review)
        await s.refresh(conf)
        assert conf.proof_path is None      # path cleared, row KEPT
        assert (await s.get(Payment, conf.id)) is not None
    _run(body, tmp_path, "d2.db")


def test_bot_users_tirekickers_pruned_registered_kept(tmp_path):
    async def body(s):
        panel = Panel(key="p", host="h", proxy_path_enc="x", owner_uuid="o")
        s.add(panel)
        await s.flush()
        # a registered reseller bound to telegram id 111
        s.add(Reseller(panel_id=panel.id, admin_uuid="a", name="R", bot_chat_id=111))
        stale = BotUser(telegram_id=999, last_seen_at=OLD, last_kicked_at=OLD)
        stale.created_at = OLD  # type: ignore[attr-defined]
        registered = BotUser(telegram_id=111, last_seen_at=OLD)  # old but registered → keep
        registered.created_at = OLD  # type: ignore[attr-defined]
        recent = BotUser(telegram_id=222, last_seen_at=NOW)      # recent → keep
        recent.created_at = NOW  # type: ignore[attr-defined]
        s.add_all([stale, registered, recent])
        await s.commit()
        c = await maintenance.prune_owner_data(s, now=NOW)
        assert c["bot_users"] == 1
        left = set((await s.execute(select(BotUser.telegram_id))).scalars().all())
        assert left == {111, 222}
    _run(body, tmp_path, "d3.db")


def test_retention_zero_still_cleans_nonces_but_not_files(tmp_path):
    async def body(s):
        await settings_service.set_value(s, "owner_data_retention_days", 0)
        s.add(PortalLoginNonce(jti="dead", expires_at=NOW - dt.timedelta(hours=1)))
        panel = Panel(key="p", host="h", proxy_path_enc="x", owner_uuid="o")
        s.add(panel)
        await s.flush()
        r = Reseller(panel_id=panel.id, admin_uuid="a", name="R")
        s.add(r)
        await s.flush()
        proof = tmp_path / "keep.jpg"
        proof.write_bytes(b"z")
        pay = Payment(reseller_id=r.id, method=PaymentMethod.screenshot,
                      status=PaymentStatus.confirmed, proof_path=str(proof), verified_at=OLD,
                      amount_usdt=0)
        pay.created_at = OLD  # type: ignore[attr-defined]
        s.add(pay)
        await s.commit()
        c = await maintenance.prune_owner_data(s, now=NOW)
        assert c["nonces"] == 1 and c["proof_files"] == 0
        assert proof.exists()  # disabled → file kept
    _run(body, tmp_path, "d4.db")


def test_invoice_pdf_sweep(tmp_path, monkeypatch):
    async def body(s):
        monkeypatch.chdir(tmp_path)
        old_dir = tmp_path / "data" / "invoices" / "2025-01"
        old_dir.mkdir(parents=True)
        old_pdf = old_dir / "factor_old.pdf"
        old_pdf.write_bytes(b"%PDF old")
        old_ts = (NOW - dt.timedelta(days=400)).timestamp()
        os.utime(old_pdf, (old_ts, old_ts))
        c = await maintenance.prune_owner_data(s, now=NOW)
        assert c["invoice_pdfs"] == 1
        assert not old_pdf.exists()
    _run(body, tmp_path, "d5.db")
