"""H06 — TON hex txid canonicalization.

- submit lowercases a hex-form TON hash so `ABC…` then `abc…` is caught as a duplicate
  (one on-chain transfer can't become two settling rows); base64 forms stay case-sensitive.
- the data migration lowercases existing hex TON rows and, on a case-collision, keeps the
  more-settled row on the canonical txid and NULLs + tags the loser (never changing a
  payment status).
"""
import asyncio
import datetime as dt
import os
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/tontxid.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import Invoice, Panel, Payment, Reseller  # noqa: E402
from app.models.enums import InvoiceStatus, PaymentMethod, PaymentStatus  # noqa: E402
from app.services import payments  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALEMBIC = str(Path(sys.executable).with_name("alembic"))
HEX_UP = "AB" * 32   # 64 hex chars, uppercase
HEX_LO = HEX_UP.lower()


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
    p = Panel(id=1, key="p", host="h.invalid", proxy_path_enc="x", owner_uuid="o")
    s.add(p)
    await s.flush()
    r = Reseller(panel_id=1, admin_uuid="a", name="R", bot_chat_id=1)
    s.add(r)
    await s.flush()
    inv = Invoice(reseller_id=r.id, panel_id=1, period_start=dt.date(2026, 1, 1),
                  period_end=dt.date(2026, 1, 28), period_label="2026-01",
                  usage_gb=10, amount_toman=100_000, amount_usdt=1,
                  status=InvoiceStatus.sent, sent_at=dt.datetime.now(dt.timezone.utc))
    s.add(inv)
    await s.commit()
    return r, inv


def test_hex_ton_txid_lowercased_and_deduped(tmp_path):
    async def body(s):
        r, inv = await _seed(s)
        first = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid=HEX_UP, chain="ton")
        assert first.status == "ok"
        assert first.payment.txid == HEX_LO            # stored lowercase
        # Re-submitting the SAME transfer in lowercase is now caught as a duplicate.
        dup = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid=HEX_LO, chain="ton")
        assert dup.status == "dup_pending"

    _run(body, tmp_path, "c1.db")


def test_base64_ton_txid_case_preserved(tmp_path):
    async def body(s):
        r, inv = await _seed(s)
        b64 = "te6cckEBAQEAAgACAErjAwXYZ_pq-Rst1234567890AB="  # 48-char base64url, mixed case
        res = await payments.submit_reseller_payment(
            s, reseller_ids={r.id}, invoice_ids=[inv.id], txid=b64, chain="ton")
        assert res.status == "ok"
        assert res.payment.txid == b64                 # NOT lowercased

    _run(body, tmp_path, "c2.db")


def _seed_collision_db(db: Path) -> None:
    subprocess.run([ALEMBIC, "upgrade", "b1c3e5a7f9d2"], cwd=BACKEND_DIR,
                   env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}"},
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            s.add(Panel(id=1, key="p", name="p", host="h", proxy_path_enc="x", owner_uuid="o"))
            s.add(Reseller(id=1, panel_id=1, admin_uuid="a", name="R"))
            await s.flush()
            # Same transfer, two casings: a rejected UPPER and a confirmed lower.
            s.add(Payment(id=1, reseller_id=1, method=PaymentMethod.ton_txid, chain="ton",
                          status=PaymentStatus.rejected, txid=HEX_UP))
            s.add(Payment(id=2, reseller_id=1, method=PaymentMethod.ton_txid, chain="ton",
                          status=PaymentStatus.confirmed, txid=HEX_LO))
            await s.commit()
        await engine.dispose()

    asyncio.run(go())


def test_migration_resolves_hex_collision(tmp_path):
    db = tmp_path / "coll.db"
    _seed_collision_db(db)
    subprocess.run([ALEMBIC, "upgrade", "head"], cwd=BACKEND_DIR,
                   env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}"},
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    async def check():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            keeper = await s.get(Payment, 2)             # confirmed → keeper
            loser = await s.get(Payment, 1)              # rejected → loser
            assert keeper.txid == HEX_LO
            assert keeper.status == PaymentStatus.confirmed   # status untouched
            assert loser.txid is None                    # NULLed to free the unique index
            assert loser.status == PaymentStatus.rejected     # status untouched
            assert "case-merged into #2" in (loser.note or "")
        await engine.dispose()

    asyncio.run(check())
