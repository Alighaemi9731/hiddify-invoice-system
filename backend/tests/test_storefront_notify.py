"""Shop-admin notifications — «اطلاع‌رسانی فروش».

The invariants that matter are not "the text is right" but "this can never hurt the sale it is
announcing": the per-shop switch is honoured on EVERY path, one unreachable admin does not silence
the others, and a Telegram failure is swallowed rather than surfaced to a caller that has already
committed money.
"""
from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models import (
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
    StorefrontPlan,
)
from app.services import storefront_notify


class _Bot:
    """Records what was sent. `_fan_out` only ever calls `send_message`."""

    def __init__(self, fail_for: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail_for = fail_for or set()

    async def send_message(self, chat_id: int, text: str, **_kw):  # noqa: ANN003, ANN202
        if chat_id in self.fail_for:
            raise RuntimeError("blocked by user")
        self.sent.append((chat_id, text))


def _run(body):  # noqa: ANN001, ANN202
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                await body(session, factory)
        finally:
            await engine.dispose()

    asyncio.run(go())


async def _seed(session, *, notify: bool = True, co_admins: str | None = "222,333"):  # noqa: ANN001, ANN202
    panel = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
    session.add(panel)
    await session.flush()
    reseller = Reseller(
        panel_id=panel.id, admin_uuid="r1", name="Owner", bot_chat_id=111,
        storefront_enabled=True)
    session.add(reseller)
    await session.flush()
    shop = StorefrontBot(
        reseller_id=reseller.id, panel_id=panel.id, bot_token_enc="tok",
        bot_username="shop_bot", config_version=1, co_admin_ids=co_admins,
        notify_admin_events=notify,
    )
    session.add(shop)
    await session.flush()
    plan = StorefrontPlan(
        storefront_bot_id=shop.id, title="طلایی", gb=50, days=30, price_toman=500_000,
        enabled=True, sort_order=0)
    customer = StorefrontCustomer(
        storefront_bot_id=shop.id, telegram_id=777, name="سارا", username="sara",
        wallet_balance_toman=120_000)
    session.add_all([plan, customer])
    await session.flush()
    order = StorefrontOrder(
        customer_id=customer.id, plan_id=plan.id, panel_id=panel.id, gb=50, days=30,
        price_toman=500_000, status="provisioned", label="خانه")
    session.add(order)
    await session.commit()
    return shop, customer, order


def test_a_sale_reaches_the_owner_and_every_co_admin_with_the_plan_name():
    async def body(session, factory):  # noqa: ANN001
        shop, _customer, order = await _seed(session)
        bot = _Bot()
        await storefront_notify.notify_purchase(
            sf_id=shop.id, order_id=order.id, bot=bot, session_factory=factory)
        assert [chat_id for chat_id, _ in bot.sent] == [111, 222, 333]
        text = bot.sent[0][1]
        assert "فروشِ جدید" in text
        assert "طلایی" in text                       # the plan's optional NAME
        assert "50 گیگابایت" in text and "30 روزه" in text
        assert "500,000" in text                     # what was charged
        assert "سارا" in text and "sara" in text     # who to recognise / how to reach them
        assert "120,000" in text                     # their remaining wallet balance

    _run(body)


def test_the_switch_silences_every_kind_of_event():
    """The switch is read inside `notify_shop_admins`, so no event site can honour it only
    partially — the whole point of keeping the check in one place."""
    async def body(session, factory):  # noqa: ANN001
        shop, customer, order = await _seed(session, notify=False)
        bot = _Bot()
        await storefront_notify.notify_purchase(
            sf_id=shop.id, order_id=order.id, bot=bot, session_factory=factory)
        await storefront_notify.notify_renewal(
            sf_id=shop.id, order_id=order.id, automatic=True, bot=bot, session_factory=factory)
        await storefront_notify.notify_topup(
            sf_id=shop.id, customer_id=customer.id, amount_toman=90_000, bot=bot,
            session_factory=factory)
        assert bot.sent == []

    _run(body)


def test_a_free_trial_is_never_announced_as_a_sale():
    """The owner asked for SALES. A trial is a giveaway funded by the platform, and announcing it
    as revenue would be a lie the shop admin might act on."""
    async def body(session, factory):  # noqa: ANN001
        shop, _customer, order = await _seed(session)
        order.is_trial = True
        order.price_toman = 0
        await session.commit()
        bot = _Bot()
        await storefront_notify.notify_purchase(
            sf_id=shop.id, order_id=order.id, bot=bot, session_factory=factory)
        assert bot.sent == []

    _run(body)


def test_one_blocked_admin_does_not_silence_the_others_and_never_raises():
    async def body(session, factory):  # noqa: ANN001
        shop, _customer, order = await _seed(session)
        bot = _Bot(fail_for={111})       # the owner blocked the bot
        await storefront_notify.notify_purchase(
            sf_id=shop.id, order_id=order.id, bot=bot, session_factory=factory)
        assert [chat_id for chat_id, _ in bot.sent] == [222, 333]

    _run(body)


def test_the_acting_admin_is_excluded_so_the_message_is_for_everyone_else():
    async def body(session, factory):  # noqa: ANN001
        shop, customer, _order = await _seed(session)
        bot = _Bot()
        await storefront_notify.notify_topup(
            sf_id=shop.id, customer_id=customer.id, amount_toman=90_000, bonus_toman=10_000,
            bot=bot, exclude_chat_id=222, session_factory=factory)
        assert [chat_id for chat_id, _ in bot.sent] == [111, 333]
        assert "پاداشِ کدِ شارژ" in bot.sent[0][1]
        assert "10,000" in bot.sent[0][1]

    _run(body)


def test_a_renewal_names_how_it_happened():
    async def body(session, factory):  # noqa: ANN001
        shop, _customer, order = await _seed(session)
        bot = _Bot()
        await storefront_notify.notify_renewal(
            sf_id=shop.id, order_id=order.id, automatic=True, bot=bot, session_factory=factory)
        assert "تمدیدِ خودکار" in bot.sent[0][1]
        bot.sent.clear()
        await storefront_notify.notify_renewal(
            sf_id=shop.id, order_id=order.id, automatic=False, bot=bot, session_factory=factory)
        assert "تمدید توسطِ مدیر" in bot.sent[0][1]

    _run(body)


def test_a_vanished_order_or_shop_is_a_silent_no_op():
    """These helpers run after a commit, often minutes later (the reaper). Anything missing means
    "nothing to announce", never an exception into a caller whose money already moved."""
    async def body(session, factory):  # noqa: ANN001
        shop, _customer, _order = await _seed(session)
        bot = _Bot()
        await storefront_notify.notify_purchase(
            sf_id=shop.id, order_id=999_999, bot=bot, session_factory=factory)
        await storefront_notify.notify_purchase(
            sf_id=shop.id, order_id=None, bot=bot, session_factory=factory)
        await storefront_notify.notify_topup(
            sf_id=999_999, customer_id=None, amount_toman=1, bot=bot, session_factory=factory)
        assert bot.sent == []

    _run(body)


def test_an_unnamed_plan_still_reads_correctly():
    async def body(session, factory):  # noqa: ANN001
        shop, _customer, order = await _seed(session)
        plan = await session.get(StorefrontPlan, order.plan_id)
        plan.title = ""
        await session.commit()
        bot = _Bot()
        await storefront_notify.notify_purchase(
            sf_id=shop.id, order_id=order.id, bot=bot, session_factory=factory)
        assert "50 گیگابایت · 30 روزه" in bot.sent[0][1]
        assert "🏅" not in bot.sent[0][1]

    _run(body)
