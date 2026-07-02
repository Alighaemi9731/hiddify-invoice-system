"""I10: storefront near-expiry reminders — days-left math, threshold, dedup/re-arm,
blocked-customer stamping, and the off switch."""
from __future__ import annotations

import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/sfexpiry.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import (  # noqa: E402
    EndUserSnapshot,
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
)
from app.services import settings_service, storefront_expiry  # noqa: E402


class _FakeBot:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[tuple[int, str]] = []
        self.session = self

    async def send_message(self, chat_id, text, **_kw):  # noqa: ANN001, ANN003
        if self.fail:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text))

    async def close(self) -> None:
        return None


def _factory_for(fake: _FakeBot):
    async def factory(_token: str):
        return fake

    return factory


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


async def _seed(s, *, days_left_via_snapshot: int | None = 2, order_days: int = 30):
    """One storefront with one provisioned order. If `days_left_via_snapshot` is set, a
    snapshot exists expiring that many days from today; else only the order duration."""
    panel = Panel(key="p", host="h.invalid", proxy_path_enc="x", owner_uuid="o")
    s.add(panel)
    await s.flush()
    r = Reseller(panel_id=panel.id, admin_uuid="a", name="R")
    s.add(r)
    await s.flush()
    bot = StorefrontBot(reseller_id=r.id, panel_id=panel.id,
                        bot_token_enc=crypto.encrypt("111:tok") or "",
                        enabled=True, status="active")
    s.add(bot)
    await s.flush()
    cust = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=555, name="C")
    s.add(cust)
    await s.flush()
    order = StorefrontOrder(customer_id=cust.id, panel_id=panel.id, plan_id=None,
                            label="کانفیگ من", gb=20, days=order_days, price_toman=100_000,
                            status="provisioned", panel_user_uuid="uu-1")
    s.add(order)
    await s.flush()
    if days_left_via_snapshot is not None:
        from app.services.periods import today as tehran_today

        start = tehran_today() - dt.timedelta(days=order_days - days_left_via_snapshot)
        s.add(EndUserSnapshot(panel_id=panel.id, user_uuid="uu-1", name="u",
                              added_by_uuid=r.admin_uuid, usage_limit_gb=20,
                              start_date=start, package_days=order_days, enable=True))
    await s.commit()
    return order, cust


def test_reminder_sent_within_threshold_and_deduped(tmp_path):
    async def body(s):
        order, cust = await _seed(s, days_left_via_snapshot=2)
        fake = _FakeBot()
        c1 = await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert c1["sent"] == 1
        assert fake.sent and fake.sent[0][0] == cust.telegram_id
        assert "منقضی" in fake.sent[0][1]
        await s.refresh(order)
        assert order.expiry_alerted_at is not None
        # Second run: already alerted this period → nothing sent.
        c2 = await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert c2["sent"] == 0 and c2["due"] == 0

    _run(body, tmp_path, "e1.db")


def test_renewal_rearms_the_reminder(tmp_path):
    async def body(s):
        order, _ = await _seed(s, days_left_via_snapshot=1)
        fake = _FakeBot()
        await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert len(fake.sent) == 1
        # Customer renews → last_renewed_at newer than the stamp → eligible again.
        order.last_renewed_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=1)
        await s.commit()
        c = await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert c["sent"] == 1
        assert len(fake.sent) == 2

    _run(body, tmp_path, "e2.db")


def test_far_from_expiry_not_reminded(tmp_path):
    async def body(s):
        await _seed(s, days_left_via_snapshot=15)  # threshold default = 3
        fake = _FakeBot()
        c = await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert c["checked"] == 1 and c["due"] == 0 and fake.sent == []

    _run(body, tmp_path, "e3.db")


def test_fallback_to_order_duration_when_no_snapshot(tmp_path):
    async def body(s):
        # No snapshot; order created "now" with days=2 → expires in 2 days → within threshold.
        order, _ = await _seed(s, days_left_via_snapshot=None, order_days=2)
        fake = _FakeBot()
        c = await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert c["sent"] == 1
        await s.refresh(order)
        assert order.expiry_alerted_at is not None

    _run(body, tmp_path, "e4.db")


def test_blocked_customer_is_stamped_not_retried(tmp_path):
    async def body(s):
        order, _ = await _seed(s, days_left_via_snapshot=1)
        fake = _FakeBot(fail=True)
        c = await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert c["due"] == 1 and c["failed"] == 1 and c["sent"] == 0
        await s.refresh(order)
        assert order.expiry_alerted_at is not None  # stamped anyway → no daily retry spam

    _run(body, tmp_path, "e5.db")


def test_zero_threshold_disables_the_feature(tmp_path):
    async def body(s):
        await _seed(s, days_left_via_snapshot=0)
        await settings_service.set_value(s, "storefront_expiry_notify_days", 0)
        fake = _FakeBot()
        c = await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert c == {"checked": 0, "due": 0, "sent": 0, "failed": 0}
        assert fake.sent == []

    _run(body, tmp_path, "e6.db")


def test_already_expired_not_spammed(tmp_path):
    async def body(s):
        await _seed(s, days_left_via_snapshot=-3)  # expired days ago
        fake = _FakeBot()
        c = await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert c["due"] == 0 and fake.sent == []

    _run(body, tmp_path, "e7.db")
