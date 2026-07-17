"""H10 — storefront money & co-admin completion.

- manual_adjust locks the customer row (double-tap by two admins credits once);
- add_co_admin rejects a banned customer and a non-positive id;
- broadcast's customer list excludes banned customers;
- an expiry day-count anchored on a UTC created_at uses the Tehran-local day.
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/sfmoney.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import (  # noqa: E402
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
)
from app.services import storefront, storefront_wallet  # noqa: E402


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


async def _seed_bot(s):
    p = Panel(id=1, key="p", host="h", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
    s.add(p)
    await s.flush()
    r = Reseller(panel_id=1, admin_uuid="a", name="R", bot_chat_id=42)
    s.add(r)
    await s.flush()
    bot = StorefrontBot(reseller_id=r.id, panel_id=1,
                        bot_token_enc=crypto.encrypt("1:abc") or "", bot_telegram_id=99,
                        enabled=True)
    s.add(bot)
    await s.flush()
    return bot


def test_manual_adjust_credits_and_never_negative(tmp_path):
    async def body(s):
        bot = await _seed_bot(s)
        cust = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=555,
                                  wallet_balance_toman=1000)
        s.add(cust)
        await s.commit()
        await storefront_wallet.manual_adjust(s, cust, 500, note="bonus")
        await s.refresh(cust)
        assert float(cust.wallet_balance_toman) == 1500
        # Debit beyond balance floors at 0, never negative.
        await storefront_wallet.manual_adjust(s, cust, -5000)
        await s.refresh(cust)
        assert float(cust.wallet_balance_toman) == 0

    _run(body, tmp_path, "m1.db")


# Manager (co-admin) appointment — including the banned / invalid-id / owner / cap rejections —
# is now the audited `storefront_admin.add_manager` command; its negative paths are covered in
# tests/test_storefront_admin_service.py::test_add_manager_rejects_banned_owner_full_and_invalid.


def test_broadcast_list_excludes_banned(tmp_path):
    async def body(s):
        bot = await _seed_bot(s)
        s.add(StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=1, banned=False))
        s.add(StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=2, banned=True))
        await s.commit()
        rows = await storefront.list_customers(s, bot.id)
        ids = {c.telegram_id for c in rows}
        assert ids == {1}

    _run(body, tmp_path, "m3.db")


def test_expiry_days_left_uses_tehran_day(tmp_path):
    from types import SimpleNamespace

    from app.services.storefront_expiry import _days_left

    # created 2026-06-10 01:00 UTC = 04:30 Tehran (same calendar day). A 30-day order:
    # today 2026-07-10 → 30 days from the 10th = expires on the 10th → 0 days left.
    order = SimpleNamespace(days=30, last_renewed_at=None,
                            created_at=dt.datetime(2026, 6, 10, 1, 0, tzinfo=dt.timezone.utc))
    assert _days_left(order, None, dt.date(2026, 7, 10)) == 0
    # created 2026-06-10 22:00 UTC = 2026-06-11 01:30 Tehran → the Tehran day is the 11th,
    # so raw .date() (10th) would be off by one. Expiry = 11th + 30 = 2026-07-11.
    order2 = SimpleNamespace(days=30, last_renewed_at=None,
                             created_at=dt.datetime(2026, 6, 10, 22, 0, tzinfo=dt.timezone.utc))
    assert _days_left(order2, None, dt.date(2026, 7, 11)) == 0
