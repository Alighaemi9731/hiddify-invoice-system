"""I10 + N01: storefront near-expiry reminders — days-left math, threshold, dedup/re-arm,
outcome taxonomy (blocked stamps, transient failure retries), flood-control, tenant
isolation, and the off switch."""
from __future__ import annotations

import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/sfexpiry.db")
os.environ.setdefault("SECRET_KEY", "k")

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter  # noqa: E402
from aiogram.methods import SendMessage  # noqa: E402
from sqlalchemy import select  # noqa: E402
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


def _m():
    return SendMessage(chat_id=1, text="x")


class _FakeBot:
    def __init__(self, fail: bool = False, blocked: bool = False,
                 retry_once: bool = False) -> None:
        self.fail = fail              # transient failure (network/unknown) → must NOT stamp
        self.blocked = blocked        # TelegramForbiddenError (permanent) → stamps
        self.retry_once = retry_once  # one 429 then success → delivered in the SAME run
        self._retried = False
        self.sent: list[tuple[int, str]] = []
        self.session = self

    async def send_message(self, chat_id, text, **_kw):  # noqa: ANN001, ANN003
        if self.retry_once and not self._retried:
            self._retried = True
            raise TelegramRetryAfter(method=_m(), message="flood", retry_after=0)
        if self.blocked:
            raise TelegramForbiddenError(method=_m(), message="blocked")
        if self.fail:
            raise RuntimeError("network blip")
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
    """TelegramForbiddenError is PERMANENT: stamp so the customer isn't retried daily."""
    async def body(s):
        order, _ = await _seed(s, days_left_via_snapshot=1)
        fake = _FakeBot(blocked=True)
        c = await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert c["due"] == 1 and c["blocked"] == 1 and c["sent"] == 0 and c["failed"] == 0
        await s.refresh(order)
        assert order.expiry_alerted_at is not None  # stamped anyway → no daily retry spam
        c2 = await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert c2["due"] == 0

    _run(body, tmp_path, "e5.db")


def test_transient_failure_not_stamped_then_retried(tmp_path):
    """N01 regression: a TRANSIENT send failure must NOT stamp — the next daily run
    retries and delivers. (Pre-N01 the stamp was written on ANY failure, silently
    dropping the reminder forever.)"""
    async def body(s):
        order, _ = await _seed(s, days_left_via_snapshot=1)
        fake = _FakeBot(fail=True)
        c = await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert c["due"] == 1 and c["failed"] == 1 and c["sent"] == 0
        await s.refresh(order)
        assert order.expiry_alerted_at is None       # NOT stamped → eligible tomorrow
        fake.fail = False                            # the outage clears
        c2 = await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert c2["sent"] == 1 and len(fake.sent) == 1
        await s.refresh(order)
        assert order.expiry_alerted_at is not None   # delivered → stamped

    _run(body, tmp_path, "e5b.db")


def test_flood_wait_is_retried_in_the_same_run(tmp_path):
    """N01 regression: a 429 (TelegramRetryAfter) is retried per the shared broadcast
    policy and DELIVERED in the same run. (Pre-N01 it was swallowed as a generic
    failure and the customer was stamped without ever receiving the reminder.)"""
    async def body(s):
        order, cust = await _seed(s, days_left_via_snapshot=1)
        fake = _FakeBot(retry_once=True)
        c = await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert c["sent"] == 1 and c["failed"] == 0
        assert fake.sent and fake.sent[0][0] == cust.telegram_id
        await s.refresh(order)
        assert order.expiry_alerted_at is not None

    _run(body, tmp_path, "e5c.db")


def test_cross_tenant_isolation_two_shops(tmp_path):
    """Each due order is messaged ONLY through its own storefront's bot (a customer of
    shop A must never hear from shop B's bot)."""
    async def body(s):
        # Shop A via the standard seed…
        _order_a, cust_a = await _seed(s, days_left_via_snapshot=1)
        # …plus shop B on the same panel with its own bot token, customer, and due order.
        panel = (await s.execute(select(Panel))).scalars().first()
        r2 = Reseller(panel_id=panel.id, admin_uuid="b", name="R2")
        s.add(r2)
        await s.flush()
        bot2 = StorefrontBot(reseller_id=r2.id, panel_id=panel.id,
                             bot_token_enc=crypto.encrypt("222:tok") or "",
                             enabled=True, status="active")
        s.add(bot2)
        await s.flush()
        cust_b = StorefrontCustomer(storefront_bot_id=bot2.id, telegram_id=777, name="B")
        s.add(cust_b)
        await s.flush()
        s.add(StorefrontOrder(customer_id=cust_b.id, panel_id=panel.id, plan_id=None,
                              label="سرویس ب", gb=10, days=30, price_toman=50_000,
                              status="provisioned", panel_user_uuid="uu-2"))
        from app.services.periods import today as tehran_today
        start = tehran_today() - dt.timedelta(days=29)  # 1 day left
        s.add(EndUserSnapshot(panel_id=panel.id, user_uuid="uu-2", name="u2",
                              added_by_uuid="b", usage_limit_gb=10,
                              start_date=start, package_days=30, enable=True))
        await s.commit()

        fakes: dict[str, _FakeBot] = {}

        async def factory(token: str):
            fakes.setdefault(token, _FakeBot())
            return fakes[token]

        c = await storefront_expiry.notify_expiring(s, bot_factory=factory)
        assert c["sent"] == 2 and len(fakes) == 2
        assert [cid for cid, _ in fakes["111:tok"].sent] == [cust_a.telegram_id]
        assert [cid for cid, _ in fakes["222:tok"].sent] == [cust_b.telegram_id]

    _run(body, tmp_path, "e5d.db")


def test_zero_threshold_disables_the_feature(tmp_path):
    async def body(s):
        await _seed(s, days_left_via_snapshot=0)
        await settings_service.set_value(s, "storefront_expiry_notify_days", 0)
        fake = _FakeBot()
        c = await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert c == {"checked": 0, "due": 0, "sent": 0, "blocked": 0, "failed": 0}
        assert fake.sent == []

    _run(body, tmp_path, "e6.db")


def test_already_expired_not_spammed(tmp_path):
    async def body(s):
        await _seed(s, days_left_via_snapshot=-3)  # expired days ago
        fake = _FakeBot()
        c = await storefront_expiry.notify_expiring(s, bot_factory=_factory_for(fake))
        assert c["due"] == 0 and fake.sent == []

    _run(body, tmp_path, "e7.db")


# ── I11: admin stats aggregates ───────────────────────────────────────────────

def test_stats_for_bot_aggregates(tmp_path):
    from app.models import StorefrontPlan, StorefrontWalletTxn
    from app.services import storefront as sf_service

    async def body(s):
        order, cust = await _seed(s, days_left_via_snapshot=2)
        now = dt.datetime.now(dt.timezone.utc)
        cust.last_seen_at = now
        cust.wallet_balance_toman = 50_000
        s.add_all([
            StorefrontPlan(storefront_bot_id=cust.storefront_bot_id, title="", gb=20,
                           days=30, price_toman=100_000, enabled=True, sort_order=0),
            StorefrontPlan(storefront_bot_id=cust.storefront_bot_id, title="", gb=50,
                           days=30, price_toman=200_000, enabled=False, sort_order=1),
            # This month's ledger: one purchase (negative debit), a partial refund, one
            # confirmed top-up, one pending top-up.
            StorefrontWalletTxn(customer_id=cust.id, kind="purchase",
                                amount_toman=-100_000, status="done", order_id=order.id),
            StorefrontWalletTxn(customer_id=cust.id, kind="refund",
                                amount_toman=20_000, status="done", order_id=order.id),
            StorefrontWalletTxn(customer_id=cust.id, kind="topup",
                                amount_toman=150_000, status="confirmed", method="card"),
            StorefrontWalletTxn(customer_id=cust.id, kind="topup",
                                amount_toman=80_000, status="pending", method="card"),
        ])
        await s.commit()

        st = await sf_service.stats_for_bot(s, cust.storefront_bot_id)
        assert st.customers == 1
        assert st.active_30d == 1
        assert st.plans_total == 2 and st.plans_enabled == 1
        assert st.provisioned == 1
        assert st.expiring_soon == 1                    # 2 days left <= threshold
        assert st.sales_month_toman == 80_000           # 100k purchase - 20k refund
        assert st.sales_month_count == 1
        assert st.topups_month_toman == 150_000         # confirmed only
        assert st.pending_topups == 1
        assert st.wallet_liability_toman == 50_000

    _run(body, tmp_path, "e8.db")
