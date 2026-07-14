"""Storefront Phase-1: wallet ledger correctness + owner monthly-fee (active-only)."""
import asyncio
import os
from decimal import Decimal
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/storefront.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.bot import keyboards  # noqa: E402
from app.bot.storefront import keyboards as sfkb  # noqa: E402
from app.core import crypto  # noqa: E402
from app.models import Panel, Reseller, StorefrontBot, StorefrontOrder  # noqa: E402
from app.models.storefront import (  # noqa: E402
    StorefrontCustomer,
    StorefrontPlan,
    StorefrontWalletTxn,
)
from app.services import (  # noqa: E402
    maintenance,
    storefront,
    storefront_provision,
    storefront_subscription,
    storefront_wallet,
    usercreate,
)


def _run(body, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()

    asyncio.run(go())


async def _seed(s, *, tag="1", storefront_enabled=True, with_bot=True, fee=None):
    p = Panel(key=f"p{tag}", host=f"p{tag}.invalid", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
    s.add(p)
    await s.flush()
    r = Reseller(
        panel_id=p.id, admin_uuid=f"A{tag}", name="Ali",
        storefront_enabled=storefront_enabled, storefront_monthly_fee_toman=fee,
    )
    s.add(r)
    await s.flush()
    bot = None
    if with_bot:
        bot = StorefrontBot(
            reseller_id=r.id, panel_id=p.id, bot_token_enc=crypto.encrypt("123:abc") or "",
            bot_telegram_id=int(f"99{tag}"), enabled=True,
        )
        s.add(bot)
        await s.flush()
    cust = None
    if bot is not None:
        cust = await storefront.get_or_create_customer(
            s, bot.id, SimpleNamespace(id=555, first_name="Cust", username="c")
        )
    await s.commit()
    return r, bot, cust


def test_co_admin_add_remove_and_is_shop_admin(tmp_path):
    """A shop owner can appoint co-admins; is_shop_admin then grants them management, and a
    non-admin is rejected. Owner id, duplicates, and the cap are handled."""
    async def body(s):
        r, bot, _c = await _seed(s)
        r.bot_chat_id = 111  # the owning reseller's Telegram id
        await s.commit()

        # No co-admins yet: only the owner is an admin.
        assert storefront.co_admin_ids(bot) == []
        assert storefront.is_shop_admin(bot, r, 111) is True
        assert storefront.is_shop_admin(bot, r, 222) is False

        # Appoint 222.
        assert await storefront.add_co_admin(s, bot, 222) == "ok"
        assert storefront.co_admin_ids(bot) == [222]
        assert storefront.is_shop_admin(bot, r, 222) is True      # co-admin can now manage
        # Idempotent + guards.
        assert await storefront.add_co_admin(s, bot, 222) == "exists"
        assert await storefront.add_co_admin(s, bot, 111) == "is_owner"

        # Cap at MAX_CO_ADMINS.
        for extra in range(300, 300 + storefront.MAX_CO_ADMINS):
            await storefront.add_co_admin(s, bot, extra)
        assert await storefront.add_co_admin(s, bot, 999) == "full"

        # Remove.
        assert await storefront.remove_co_admin(s, bot, 222) is True
        assert 222 not in storefront.co_admin_ids(bot)
        assert storefront.is_shop_admin(bot, r, 222) is False
        assert await storefront.remove_co_admin(s, bot, 222) is False  # already gone

    _run(body, tmp_path, "coadmin.db")


def test_shop_closed_blocks_buy(tmp_path):
    """A «temporarily closed» shop shows the closed message and does NOT list plans; an open shop
    lists plans as before. Also covers the closed-text helper (custom vs default)."""
    from app.bot.storefront import handlers as H
    from app.models import StorefrontPlan

    async def body(s):
        _r, bot, cust = await _seed(s)
        s.add(StorefrontPlan(storefront_bot_id=bot.id, title="P", gb=10, days=30,
                             price_toman=50000, enabled=True))
        await s.commit()

        assert H._shop_closed_text(bot) == H._SHOP_CLOSED_DEFAULT      # default when unset
        bot.closed_text = "فعلاً بسته‌ایم"
        assert H._shop_closed_text(bot) == "فعلاً بسته‌ایم"            # custom text

        sent: list = []

        class FakeMsg:
            async def answer(self, text: str = "", **kw):  # noqa: ANN003
                sent.append((text, kw.get("reply_markup")))

        # Closed → one message, NO plans keyboard.
        bot.shop_closed = True
        await s.commit()
        await H._customer_action("buy", FakeMsg(), None, s, bot, cust, None)
        assert len(sent) == 1 and sent[0][1] is None

        # Open → plans are listed (a keyboard is attached).
        bot.shop_closed = False
        await s.commit()
        sent.clear()
        await H._customer_action("buy", FakeMsg(), None, s, bot, cust, None)
        assert len(sent) == 1 and sent[0][1] is not None

    _run(body, tmp_path, "shopclosed.db")


class _NoticeBotSession:
    async def close(self):
        return None


class _NoticeBot:
    def __init__(self):
        self.sent: list = []
        self.session = _NoticeBotSession()

    async def send_message(self, chat_id, text, **kw):  # noqa: ANN003
        self.sent.append((chat_id, text, kw.get("reply_markup")))


def test_notify_trial_ended(tmp_path):
    """#3 — an expired free trial gets one «trial ended → buy» nudge, then dedups."""
    import datetime as _dt

    from app.models import StorefrontOrder
    from app.services import storefront_expiry

    async def body(s):
        _r, bot, cust = await _seed(s)
        now = _dt.datetime.now(_dt.timezone.utc)
        s.add(StorefrontOrder(customer_id=cust.id, panel_id=bot.panel_id, plan_id=None,
                              gb=1, days=1, price_toman=0, status="provisioned",
                              panel_user_uuid="tu", is_trial=True,
                              created_at=now - _dt.timedelta(days=5)))
        await s.commit()

        made: list = []

        async def factory(token):
            b = _NoticeBot()
            made.append(b)
            return b

        c = await storefront_expiry.notify_trial_ended(s, bot_factory=factory)
        assert c["sent"] == 1 and made[0].sent[0][0] == cust.telegram_id
        assert made[0].sent[0][2] is not None                # buy button attached
        c2 = await storefront_expiry.notify_trial_ended(s, bot_factory=factory)
        assert c2["due"] == 0                                # dedup: not sent again

    _run(body, tmp_path, "trialended.db")


def test_notify_usage_high(tmp_path):
    """#7 — a paid config at ≥80% usage gets one renew warning; dedups; re-arms after a renewal."""
    import datetime as _dt

    from app.models import EndUserSnapshot, StorefrontOrder
    from app.services import storefront_expiry

    async def body(s):
        _r, bot, cust = await _seed(s)
        order = StorefrontOrder(customer_id=cust.id, panel_id=bot.panel_id, plan_id=1,
                                gb=10, days=30, price_toman=50000, status="provisioned",
                                panel_user_uuid="pu", is_trial=False)
        s.add(order)
        s.add(EndUserSnapshot(panel_id=bot.panel_id, user_uuid="pu", added_by_uuid="x",
                              usage_limit_gb=10, current_usage_gb=8.5, enable=True))
        await s.commit()

        made: list = []

        async def factory(token):
            b = _NoticeBot()
            made.append(b)
            return b

        c = await storefront_expiry.notify_usage_high(s, bot_factory=factory)
        assert c["sent"] == 1 and made[0].sent[0][0] == cust.telegram_id
        c2 = await storefront_expiry.notify_usage_high(s, bot_factory=factory)
        assert c2["due"] == 0                                       # dedup
        # a renewal re-arms the warning
        await s.refresh(order)
        order.last_renewed_at = _dt.datetime.now(_dt.timezone.utc)
        await s.commit()
        c3 = await storefront_expiry.notify_usage_high(s, bot_factory=factory)
        assert c3["due"] == 1                                       # re-armed

    _run(body, tmp_path, "usagehigh.db")


def test_customers_in_segment(tmp_path):
    """#8 — each broadcast segment selects the right non-banned customers."""
    import datetime as _dt

    from app.models import StorefrontCustomer, StorefrontOrder

    async def body(s):
        _r, bot, _c0 = await _seed(s)
        now = _dt.datetime.now(_dt.timezone.utc)
        active = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=1001, last_seen_at=now)
        inactive = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=1002,
                                      last_seen_at=now - _dt.timedelta(days=40))
        trial = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=1003,
                                   last_seen_at=now, free_trial_used=True)
        trial_bought = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=1004,
                                          last_seen_at=now, free_trial_used=True)
        expired = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=1005, last_seen_at=now)
        banned = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=1006, banned=True)
        s.add_all([active, inactive, trial, trial_bought, expired, banned])
        await s.flush()
        # a paid config for trial_bought → excluded from trial_no_purchase
        s.add(StorefrontOrder(customer_id=trial_bought.id, panel_id=bot.panel_id, plan_id=1,
                              gb=10, days=30, price_toman=50000, status="provisioned",
                              panel_user_uuid="pu-bought", is_trial=False))
        # an expired config for `expired` (created 40d ago, 1-day plan, no snapshot → fallback math)
        s.add(StorefrontOrder(customer_id=expired.id, panel_id=bot.panel_id, plan_id=None,
                              gb=10, days=1, price_toman=0, status="provisioned",
                              panel_user_uuid="pu-exp", is_trial=False,
                              created_at=now - _dt.timedelta(days=40)))
        await s.commit()

        def tids(rows):
            return sorted(c.telegram_id for c in rows)

        all_ = tids(await storefront.customers_in_segment(s, bot.id, "all"))
        assert 1006 not in all_ and {1001, 1002, 1003, 1004, 1005}.issubset(set(all_))
        inact = tids(await storefront.customers_in_segment(s, bot.id, "inactive30"))
        assert 1002 in inact and 1001 not in inact
        tnp = tids(await storefront.customers_in_segment(s, bot.id, "trial_no_purchase"))
        assert 1003 in tnp and 1004 not in tnp
        exp = tids(await storefront.customers_in_segment(s, bot.id, "expired"))
        assert exp == [1005]

    _run(body, tmp_path, "segments.db")


def test_topup_confirm_credits_once_and_purchase_is_atomic(tmp_path):
    async def body(s):
        _r, _bot, cust = await _seed(s)
        txn = await storefront_wallet.create_topup(s, cust, 300_000, method="card")
        assert txn.status == "pending"
        await s.refresh(cust)
        assert storefront_wallet.balance(cust) == Decimal(0)          # not credited while pending

        changed, _ = await storefront_wallet.confirm_topup(s, txn.id)
        await s.refresh(cust)
        assert changed and storefront_wallet.balance(cust) == Decimal(300_000)

        # double-confirm must NOT double-credit
        changed2, _ = await storefront_wallet.confirm_topup(s, txn.id)
        await s.refresh(cust)
        assert changed2 is False and storefront_wallet.balance(cust) == Decimal(300_000)

        # purchase debits atomically; a too-expensive buy is refused (balance untouched)
        ok, _ = await storefront_wallet.charge_purchase(s, cust.id, 200_000)
        await s.refresh(cust)
        assert ok and storefront_wallet.balance(cust) == Decimal(100_000)
        broke, _ = await storefront_wallet.charge_purchase(s, cust.id, 200_000)
        await s.refresh(cust)
        assert broke is False and storefront_wallet.balance(cust) == Decimal(100_000)

    _run(body, tmp_path, "wallet.db")


def test_reject_topup_and_manual_adjust_floor(tmp_path):
    async def body(s):
        _r, _bot, cust = await _seed(s)
        txn = await storefront_wallet.create_topup(s, cust, 50_000, method="card")
        changed, _ = await storefront_wallet.reject_topup(s, txn.id)
        await s.refresh(cust)
        assert changed and storefront_wallet.balance(cust) == Decimal(0)  # rejected → no credit

        await storefront_wallet.manual_adjust(s, cust, 80_000, note="gift")
        await s.refresh(cust)
        assert storefront_wallet.balance(cust) == Decimal(80_000)
        # a debit larger than the balance floors at 0 (never negative)
        await storefront_wallet.manual_adjust(s, cust, -200_000, note="correction")
        await s.refresh(cust)
        assert storefront_wallet.balance(cust) == Decimal(0)

    _run(body, tmp_path, "wallet2.db")


def test_topup_decided_once_cannot_be_flipped(tmp_path):
    """Once one admin confirms (or rejects) a top-up, a second admin's opposite decision is a no-op —
    no double credit and no flip (covers two admins acting on the same fish)."""
    async def body(s):
        _r, _bot, cust = await _seed(s)
        # confirm → a later reject must NOT un-credit or change the status
        t1 = await storefront_wallet.create_topup(s, cust, 100_000, method="card")
        await storefront_wallet.confirm_topup(s, t1.id)
        changed, t1b = await storefront_wallet.reject_topup(s, t1.id)
        await s.refresh(cust)
        assert changed is False and t1b.status == "confirmed"
        assert storefront_wallet.balance(cust) == Decimal(100_000)

        # reject → a later confirm must NOT credit
        t2 = await storefront_wallet.create_topup(s, cust, 50_000, method="card")
        await storefront_wallet.reject_topup(s, t2.id)
        changed2, t2b = await storefront_wallet.confirm_topup(s, t2.id)
        await s.refresh(cust)
        assert changed2 is False and t2b.status == "rejected"
        assert storefront_wallet.balance(cust) == Decimal(100_000)  # unchanged

    _run(body, tmp_path, "wallet3.db")


def test_admin_chat_ids_includes_owner_and_co_admins():
    """Admin notifications fan out to the owner AND every co-admin (deduped)."""
    from app.bot.storefront.handlers import _admin_chat_ids
    owner = SimpleNamespace(bot_chat_id=111)
    assert _admin_chat_ids(owner, SimpleNamespace(co_admin_ids="222,333")) == [111, 222, 333]
    assert _admin_chat_ids(owner, SimpleNamespace(co_admin_ids="111,222")) == [111, 222]  # dedup
    assert _admin_chat_ids(owner, None) == [111]                                          # no sf
    assert _admin_chat_ids(owner, SimpleNamespace(co_admin_ids=None)) == [111]            # no co-admins
    # owner who never registered in the bot (no chat id) → only the co-admins receive
    assert _admin_chat_ids(SimpleNamespace(bot_chat_id=None),
                           SimpleNamespace(co_admin_ids="222")) == [222]


def test_storefront_telegram_id_columns_are_bigint():
    # Telegram bot/user ids exceed int32; the columns MUST be BigInteger or Postgres rejects them
    # ("value out of int32 range"). SQLite has no int32 cap, so only a metadata check catches this.
    assert isinstance(StorefrontBot.__table__.c.bot_telegram_id.type, BigInteger)
    assert isinstance(StorefrontCustomer.__table__.c.telegram_id.type, BigInteger)


def test_customer_with_id_above_int32_roundtrips(tmp_path):
    async def body(s):
        _r, bot, _c = await _seed(s)
        big = 8_640_657_004  # a real-world Telegram id, > int32 max (2_147_483_647)
        cust = await storefront.get_or_create_customer(
            s, bot.id, SimpleNamespace(id=big, first_name="Big", username="b")
        )
        await s.commit()
        assert cust.telegram_id == big
        again = await storefront.get_or_create_customer(
            s, bot.id, SimpleNamespace(id=big, first_name="Big", username="b")
        )
        assert again.id == cust.id  # same customer, no duplicate

    _run(body, tmp_path, "bigid.db")


def test_reseller_menu_keyboard_storefront_button():
    on = keyboards.reseller_menu_keyboard(show_storefront=True)
    cbs = [b.callback_data for row in on.inline_keyboard for b in row]
    assert "menu:storefront" in cbs
    off = keyboards.reseller_menu_keyboard(show_storefront=False)
    cbs_off = [b.callback_data for row in off.inline_keyboard for b in row]
    assert "menu:storefront" not in cbs_off


def test_plan_label_is_gb_days_price_never_title():
    p = StorefrontPlan(title="اقتصادی", gb=30, days=30, price_toman=120000)
    label = sfkb.plan_label(p)
    assert "اقتصادی" not in label          # owner: «عنوان نمی‌خواهیم»
    assert "30 گیگ" in label and "30 روزه" in label and "120,000" in label


def test_update_plan_edits_and_rejects_foreign(tmp_path):
    async def body(s):
        _r, bot, _c = await _seed(s)
        p = await storefront.add_plan(s, bot.id, title="", gb=10, days=30, price_toman=50_000)
        assert await storefront.update_plan(s, bot.id, p.id, gb=20, days=60, price_toman=90_000)
        p2 = await s.get(StorefrontPlan, p.id)
        assert (p2.gb, p2.days, p2.price_toman) == (20, 60, 90_000)
        # a different storefront can't edit this plan
        assert await storefront.update_plan(
            s, bot.id + 999, p.id, gb=1, days=1, price_toman=1) is False

    _run(body, tmp_path, "planedit.db")


def test_move_plan_reorders(tmp_path):
    async def body(s):
        _r, bot, _c = await _seed(s)
        a = await storefront.add_plan(s, bot.id, title="", gb=1, days=1, price_toman=1)
        b = await storefront.add_plan(s, bot.id, title="", gb=2, days=2, price_toman=2)
        c = await storefront.add_plan(s, bot.id, title="", gb=3, days=3, price_toman=3)

        async def order():
            return [p.id for p in await storefront.list_plans(s, bot.id)]

        assert await order() == [a.id, b.id, c.id]
        assert await storefront.move_plan(s, bot.id, c.id, "up")     # a, c, b
        assert await order() == [a.id, c.id, b.id]
        assert await storefront.move_plan(s, bot.id, a.id, "down")   # c, a, b
        assert await order() == [c.id, a.id, b.id]
        # edge no-op: the top plan can't move up
        top = (await storefront.list_plans(s, bot.id))[0]
        assert await storefront.move_plan(s, bot.id, top.id, "up") is False

    _run(body, tmp_path, "planmove.db")


def test_plans_manage_kb_has_edit_move_delete():
    plans = [StorefrontPlan(id=1, gb=1, days=1, price_toman=1, enabled=True, sort_order=0),
             StorefrontPlan(id=2, gb=2, days=2, price_toman=2, enabled=True, sort_order=1)]
    data = [b.callback_data for row in sfkb.plans_manage_kb(plans).inline_keyboard for b in row]
    assert "sfplanedit:1" in data and "sfplandel:2" in data
    assert "sfplandown:1" in data          # first can move down, not up
    assert "sfplanup:1" not in data
    assert "sfplanup:2" in data            # last can move up, not down
    assert "sfplandown:2" not in data
    assert "sfplanadd" in data


def test_storefront_order_has_label_column(tmp_path):
    async def body(s):
        _r, _bot, cust = await _seed(s)
        o = StorefrontOrder(customer_id=cust.id, label="گوشی", gb=30, days=30,
                            price_toman=1000, status="provisioned")
        s.add(o)
        await s.commit()
        await s.refresh(o)
        assert o.label == "گوشی"

    _run(body, tmp_path, "orderlabel.db")


def test_provision_passes_customer_label_as_config_name(tmp_path, monkeypatch):
    captured = {}

    async def fake_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001, ANN002
        captured["base_name"] = base_name
        captured["count"] = count
        uid = user_uuid or "u-1"
        return SimpleNamespace(
            created=[SimpleNamespace(name=base_name, uuid=uid,
                                     sub_link=f"https://h/p/{uid}/#{base_name}")],
            error=None, capacity_blocked=False, limit_hit=False)

    monkeypatch.setattr(usercreate, "create_for_reseller", fake_create)

    async def body(s):
        _r, bot, cust = await _seed(s)
        res = await storefront_provision.provision(s, bot, cust, gb=30, days=30, label="گوشی")
        assert res.ok and res.uuid == "u-1"
        assert captured["base_name"] == "گوشی"   # exactly the customer's text (Q1)
        assert captured["count"] == 1

    _run(body, tmp_path, "provlabel.db")


def test_live_status_maps_panel_fields(tmp_path, monkeypatch):
    from app.services.panel_client import admin_api

    async def fake_get_user(self, panel, uuid, *, api_key=None):  # noqa: ANN001, ANN002
        return {"current_usage_GB": 3.5, "usage_limit_GB": 30, "remaining_day": 12}

    monkeypatch.setattr(admin_api.AdminApiClient, "get_user", fake_get_user)

    async def body(s):
        _r, bot, cust = await _seed(s)
        order = StorefrontOrder(customer_id=cust.id, label="x", gb=30, days=30,
                                price_toman=1000, status="provisioned", panel_user_uuid="u-1")
        s.add(order)
        await s.commit()
        st = await storefront_provision.live_status(s, bot, order)
        assert st.ok and st.used_gb == 3.5 and st.limit_gb == 30.0 and st.remaining_days == 12

        order.panel_user_uuid = None            # no uuid → best-effort failure, not a crash
        st2 = await storefront_provision.live_status(s, bot, order)
        assert st2.ok is False

    _run(body, tmp_path, "livestatus.db")


def test_wallet_kb_offers_topup_then_amount():
    cbs = [b.callback_data for row in sfkb.wallet_kb().inline_keyboard for b in row]
    assert "sftopup" in cbs                     # wallet screen offers top-up (not an immediate prompt)


def test_orders_kb_label_is_rtl_isolated():
    o = StorefrontOrder(id=1, customer_id=1, label="phone", gb=30, days=30,
                        price_toman=0, status="provisioned")
    text = sfkb.orders_kb([o]).inline_keyboard[0][0].text
    assert "⁨phone⁩" in text                     # English name isolated → order doesn't scramble


def test_customer_kb_shows_free_trial_only_when_flagged():
    on = [b.text for row in sfkb.customer_reply_kb(show_free_trial=True).keyboard for b in row]
    off = [b.text for row in sfkb.customer_reply_kb(show_free_trial=False).keyboard for b in row]
    assert sfkb.FREE_TRIAL_LABEL in on and sfkb.FREE_TRIAL_LABEL not in off


def test_customer_detail_kb_relay_button_for_everyone():
    """Reaching a customer is a bot-RELAY button («پیام به مشتری» → sfmsg:<id>) present for EVERY
    customer — username or not — so the card is consistent and never has a dead link. A public
    @username ADDS a direct-PV shortcut (t.me), but a username-less customer gets NO tg://user?id=
    link (Telegram can't resolve it for the admin → it was a dead link, the reported bug)."""
    for username, chat_id in [("@Ali_Shop", 555), (None, 555), ("نام فارسی", 555), (None, None)]:
        m = sfkb.customer_detail_kb(7, username=username, chat_id=chat_id)
        cbs = [b.callback_data for row in m.inline_keyboard for b in row if b.callback_data]
        urls = [b.url for row in m.inline_keyboard for b in row if b.url]
        assert "sfmsg:7" in cbs                              # the universal relay button
        assert not any((u or "").startswith("tg://") for u in urls)   # never a dead tg:// link
        assert "sfadj:7:+" in cbs and "sfacust:7" in cbs and "sfcustpg:0" in cbs

    # a valid public username ALSO gets the direct-PV t.me shortcut
    with_u = sfkb.customer_detail_kb(7, username="@Ali_Shop", chat_id=555)
    assert "https://t.me/Ali_Shop" in [b.url for row in with_u.inline_keyboard for b in row if b.url]
    # no/invalid username → no url button at all (relay only)
    no_u = sfkb.customer_detail_kb(7, username=None, chat_id=555)
    assert [b.url for row in no_u.inline_keyboard for b in row if b.url] == []
    bad = sfkb.customer_detail_kb(7, username="نام فارسی", chat_id=555)
    assert [b.url for row in bad.inline_keyboard for b in row if b.url] == []


def test_relay_reply_kb():
    """A relayed customer message carries a «پاسخ» button that reuses the compose flow (sfmsg:<id>)."""
    cbs = [b.callback_data for row in sfkb.relay_reply_kb(7).inline_keyboard for b in row]
    assert cbs == ["sfmsg:7"]


class _RelayBot:
    """Records send_message/send_photo; optionally raises to simulate a blocked/unreachable target."""
    def __init__(self, *, forbidden: bool = False, error: bool = False):
        self.forbidden, self.error, self.sent = forbidden, error, []

    async def send_message(self, chat_id, text, **kw):  # noqa: ANN001
        if self.forbidden:
            from aiogram.exceptions import TelegramForbiddenError
            raise TelegramForbiddenError(method=None, message="bot was blocked by the user")
        if self.error:
            raise RuntimeError("transient")
        self.sent.append((chat_id, text, kw.get("reply_markup")))

    async def send_photo(self, chat_id, photo, caption=None, **kw):  # noqa: ANN001
        if self.forbidden:
            from aiogram.exceptions import TelegramForbiddenError
            raise TelegramForbiddenError(method=None, message="blocked")
        if self.error:
            raise RuntimeError("transient")
        self.sent.append((chat_id, caption, kw.get("reply_markup")))


def test_relay_message_delivers_and_reports_unreachable():
    """_relay_message forwards text to the target (True); a blocked target OR a transient error → False,
    never raising (the handler must survive)."""
    import asyncio as _asyncio

    from app.bot.storefront import handlers as h

    msg = SimpleNamespace(text="سلام مشتری عزیز", photo=None, caption=None)
    bot = _RelayBot()
    assert _asyncio.run(h._relay_message(bot, 863, "📨 پیام از پشتیبانی:", msg)) is True
    assert bot.sent and bot.sent[0][0] == 863 and "سلام مشتری عزیز" in bot.sent[0][1]

    assert _asyncio.run(h._relay_message(_RelayBot(forbidden=True), 863, "📨", msg)) is False
    assert _asyncio.run(h._relay_message(_RelayBot(error=True), 863, "📨", msg)) is False


def test_relay_to_admins_reaches_all_with_reply_button():
    """A customer message is delivered to EVERY admin id, each carrying a «پاسخ» (sfmsg:<id>) button."""
    import asyncio as _asyncio

    from app.bot.storefront import handlers as h

    msg = SimpleNamespace(text="کمک می‌خوام", photo=None, caption=None)
    bot = _RelayBot()
    ok = _asyncio.run(h._relay_to_admins(bot, [111, 222], "Mahsa", 7, msg))
    assert ok is True
    assert sorted(chat for chat, _t, _m in bot.sent) == [111, 222]
    for _chat, _text, markup in bot.sent:
        cbs = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert cbs == ["sfmsg:7"]
    # no admins at all → nothing delivered, no crash
    assert _asyncio.run(h._relay_to_admins(_RelayBot(), [], "Mahsa", 7, msg)) is False


class _FakeState:
    def __init__(self, st=None):
        self._st, self.data = st, {}

    async def get_state(self):
        return self._st

    async def clear(self):
        self._st, self.data = None, {}

    async def set_state(self, st):  # noqa: ANN001
        self._st = st.state if hasattr(st, "state") else st

    async def update_data(self, **kw):  # noqa: ANN003
        self.data.update(kw)

    async def get_data(self):
        return dict(self.data)


def _fallback_harness(tmp_path, monkeypatch, name):
    """Wire an in-memory storefront (reseller admin id 111, one customer id 555) and point the
    handlers module at it. Returns (run, relayed, answered) where run(state, msg) drives sf_fallback."""
    from app.bot.storefront import handlers as H

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    from app.core.db import Base

    relayed: list = []
    answered: list = []

    class FakeBot:
        id = 991  # matches _seed's bot_telegram_id for tag "1" (int("99"+"1"))

        async def send_message(self, chat_id, text, **kw):  # noqa: ANN001, ANN003
            relayed.append((chat_id, text, kw.get("reply_markup")))

        async def send_photo(self, chat_id, photo, caption=None, **kw):  # noqa: ANN001, ANN003
            relayed.append((chat_id, caption, kw.get("reply_markup")))

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            r, bot, _c = await _seed(s)
            r.bot_chat_id = 111
            await s.commit()
        monkeypatch.setattr(H, "SessionLocal", Session)

    async def run(state, msg):
        await H.sf_fallback(msg, state, FakeBot())

    return setup, run, relayed, answered, engine


def _fake_customer_msg(answered, *, text=None, photo=None):
    class FakeMsg:
        from_user = SimpleNamespace(id=555, first_name="Cust", username="c")

        async def answer(self, t, **kw):  # noqa: ANN001, ANN003
            answered.append(t)

    m = FakeMsg()
    m.text, m.photo, m.caption = text, photo, None
    return m


def test_sf_fallback_relays_idle_customer_with_light_ack(tmp_path, monkeypatch):
    """An IDLE customer's free text is relayed to the admin (with a «پاسخ» button) and the customer
    gets ONE light ack — NOT the full welcome/balance menu (which would clutter a back-and-forth)."""
    setup, run, relayed, answered, engine = _fallback_harness(tmp_path, monkeypatch, "fb1.db")

    async def go():
        await setup()
        try:
            await run(_FakeState(None), _fake_customer_msg(answered, text="سرویسم قطع شده"))
        finally:
            await engine.dispose()

    asyncio.run(go())
    assert any(chat == 111 for chat, _t, _m in relayed)            # admin got it
    assert any(m is not None for _c, _t, m in relayed)             # with a reply button
    assert len(answered) == 1 and "پشتیبانی" in answered[0]        # light ack, no full menu re-render


def test_sf_fallback_does_not_relay_mid_flow_message(tmp_path, monkeypatch):
    """A message that fell through mid-flow (a wrong-type message during a compose/FSM state) must NOT
    be relayed as support — it aborts to the menu, and the leaked state is cleared."""
    setup, run, relayed, answered, engine = _fallback_harness(tmp_path, monkeypatch, "fb2.db")

    async def go():
        await setup()
        try:
            st = _FakeState("SF:buy_name")                        # a half-open flow
            await run(st, _fake_customer_msg(answered, text="یک عکس فرستادم"))
            assert await st.get_state() is None                   # state was cleared (no leak)
        finally:
            await engine.dispose()

    asyncio.run(go())
    assert relayed == []                                          # nothing relayed to the admin
    assert len(answered) >= 1                                     # the menu WAS re-shown instead


def test_sf_cmd_cancel_clears_state_and_shows_menu(tmp_path, monkeypatch):
    """/cancel is a real global cancel: it clears any in-progress compose and re-shows the menu,
    instead of being relayed to the customer as literal text."""
    from app.bot.storefront import handlers as H

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancel.db'}")
    from app.core.db import Base

    answered: list = []

    class FakeBot:
        id = 991

    class FakeMsg:
        from_user = SimpleNamespace(id=555, first_name="Cust", username="c")
        text = "/cancel"

        async def answer(self, t, **kw):  # noqa: ANN001, ANN003
            answered.append(t)

    async def go():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            r, _bot, _c = await _seed(s)
            r.bot_chat_id = 111
            await s.commit()
        monkeypatch.setattr(H, "SessionLocal", Session)
        st = _FakeState("SF:dm_compose")
        try:
            await H.sf_cmd_cancel(FakeMsg(), st, FakeBot())
            assert await st.get_state() is None                   # compose cleared
        finally:
            await engine.dispose()

    asyncio.run(go())
    assert len(answered) == 1                                     # menu shown (not a relay)


def test_customer_detail_kb_ban_toggle():
    """The detail keyboard shows «مسدود کردن» when active and «رفعِ مسدودی» when banned."""
    active = sfkb.customer_detail_kb(7, chat_id=555, banned=False)
    cbs = [b.callback_data for row in active.inline_keyboard for b in row if b.callback_data]
    assert "sfcustban:7" in cbs and "sfcustunban:7" not in cbs
    banned = sfkb.customer_detail_kb(7, chat_id=555, banned=True)
    cbs = [b.callback_data for row in banned.inline_keyboard for b in row if b.callback_data]
    assert "sfcustunban:7" in cbs and "sfcustban:7" not in cbs


def test_customer_detail_opens_for_username_less_customer():
    """The reported prod bug: a username-less customer's card either failed to open (tg:// BUTTON
    rejected) or showed a DEAD body tg:// link the admin couldn't tap. The fix: the card carries a
    bot-relay callback button (never a tg:// link), so it opens cleanly for every customer and the
    admin can always reach them."""
    import asyncio as _asyncio

    from app.bot.storefront import handlers as h

    cust = SimpleNamespace(id=7, name="Mahsa", telegram_id=863, wallet_balance_toman=0,
                           banned=False, username=None)
    sent: list = []

    class FakeMsg:
        async def edit_text(self, text, reply_markup=None, parse_mode=None):  # noqa: ANN001
            sent.append((text, reply_markup))

        async def answer(self, text, reply_markup=None, parse_mode=None):  # noqa: ANN001
            sent.append((text, reply_markup))

    cb = SimpleNamespace(message=FakeMsg())
    _asyncio.run(h._show_customer_detail(cb, cust))

    assert len(sent) == 1                                   # the card opened, first try
    text, markup = sent[0]
    assert "tg://" not in text                              # no dead body link anymore
    urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
    assert not any((u or "").startswith("tg://") for u in urls)   # no dead button either
    cbs = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    assert "sfmsg:7" in cbs                                 # the relay button IS there
    assert "sfadj:7:+" in cbs and "sfcustban:7" in cbs      # and every other action


def test_tg_pv_url_precedence():
    from app.core.tg_links import clean_username, tg_pv_url
    assert tg_pv_url("@Ali_Shop", 555) == "https://t.me/Ali_Shop"   # username wins
    assert tg_pv_url(None, 555) == "tg://user?id=555"               # falls back to id
    assert tg_pv_url("نام فارسی", 555) == "tg://user?id=555"        # invalid username → id
    assert tg_pv_url(None, None) is None
    assert clean_username("@Ali_Shop") == "Ali_Shop"
    assert clean_username("has space") is None and clean_username(None) is None
    assert sfkb.clean_username("bob123") == "bob123"                # re-exported from keyboards


def test_trial_available_logic():
    from app.bot.storefront.handlers import _trial_available
    assert _trial_available(SimpleNamespace(free_trial_enabled=True),
                            SimpleNamespace(free_trial_used=False)) is True
    assert _trial_available(SimpleNamespace(free_trial_enabled=True),
                            SimpleNamespace(free_trial_used=True)) is False
    assert _trial_available(SimpleNamespace(free_trial_enabled=False),
                            SimpleNamespace(free_trial_used=False)) is False


def _fake_create_factory(calls):  # noqa: ANN001, ANN202
    async def fake_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001, ANN002
        calls["n"] += 1
        uid = user_uuid or f"u{calls['n']}"
        return SimpleNamespace(
            created=[SimpleNamespace(name=base_name, uuid=uid, sub_link=f"https://h/p/{uid}/#x")],
            error=None, capacity_blocked=False, limit_hit=False)
    return fake_create


def _engine_session(tmp_path, name):  # noqa: ANN001, ANN202
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def test_free_trial_is_one_per_customer(tmp_path, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(usercreate, "create_for_reseller", _fake_create_factory(calls))

    async def go():
        engine, Session = _engine_session(tmp_path, "trial.db")
        from app.core.db import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            bot.free_trial_enabled, bot.free_trial_gb, bot.free_trial_days = True, 1, 1
            await s.commit()
            sf_id, cid = bot.id, cust.id

        r1 = await storefront_provision.claim_trial(Session, sf_id=sf_id, customer_id=cid)
        assert r1.ok and calls["n"] == 1
        async with Session() as s:
            assert (await s.get(StorefrontCustomer, cid)).free_trial_used is True
        # second claim refused — no second config minted
        r2 = await storefront_provision.claim_trial(Session, sf_id=sf_id, customer_id=cid)
        assert r2.ok is False and r2.reason == "used" and calls["n"] == 1
        await engine.dispose()

    asyncio.run(go())


def test_free_trial_concurrent_double_tap_mints_one(tmp_path, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(usercreate, "create_for_reseller", _fake_create_factory(calls))

    async def go():
        engine, Session = _engine_session(tmp_path, "trial2.db")
        from app.core.db import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            bot.free_trial_enabled = True
            await s.commit()
            sf_id, cid = bot.id, cust.id
        a, b = await asyncio.gather(
            storefront_provision.claim_trial(Session, sf_id=sf_id, customer_id=cid),
            storefront_provision.claim_trial(Session, sf_id=sf_id, customer_id=cid),
        )
        assert sum(1 for r in (a, b) if r.ok) == 1   # exactly one trial minted
        assert calls["n"] == 1
        await engine.dispose()

    asyncio.run(go())


def test_manager_polls_each_bot_independently_no_fleet_restart(tmp_path, monkeypatch):
    # Each bot gets its OWN poll loop; adding a bot must NOT restart the others (a fleet-wide restart
    # caused overlapping getUpdates → every update delivered twice). This guards that: the original
    # pollers are the SAME task objects after a new bot is added.
    import asyncio as _aio

    from aiogram import Bot

    from app.bot.storefront import manager

    async def fake_get_me(self):  # noqa: ANN001
        return SimpleNamespace(id=int(self.token.split(":")[0]), username="b" + self.token[:3])

    async def fake_get_updates(self, **kw):  # noqa: ANN001, ANN003 — idle long-poll, no network
        await _aio.sleep(0.2)
        return []

    monkeypatch.setattr(Bot, "get_me", fake_get_me)
    monkeypatch.setattr(Bot, "get_updates", fake_get_updates)

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'mgr.db'}")
        from app.core.db import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)

        async def add_bot(i, tok):  # noqa: ANN001, ANN202
            async with Session() as s:
                p = Panel(key=f"mp{i}", host=f"mp{i}.invalid",
                          proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
                s.add(p)
                await s.flush()
                r = Reseller(panel_id=p.id, admin_uuid=f"M{i}", name=f"R{i}", storefront_enabled=True)
                s.add(r)
                await s.flush()
                s.add(StorefrontBot(reseller_id=r.id, panel_id=p.id,
                                    bot_token_enc=crypto.encrypt(tok) or "", enabled=True))
                await s.commit()

        await add_bot(1, "111:aaa")
        await add_bot(2, "222:bbb")
        monkeypatch.setattr(manager, "SessionLocal", Session)
        manager._active, manager._dp = {}, None

        await manager.reconcile()
        assert len(manager._active) == 2                       # one independent poller per bot
        tasks = {rid: r.task for rid, r in manager._active.items()}
        assert all(not t.done() for t in tasks.values())

        await manager.reconcile()                              # unchanged → SAME tasks, not restarted
        assert {rid: r.task for rid, r in manager._active.items()} == tasks

        await add_bot(3, "333:ccc")
        await manager.reconcile()
        assert len(manager._active) == 3
        for rid, t in tasks.items():                           # the original two are UNTOUCHED
            assert manager._active[rid].task is t

        for rid in list(manager._active):
            await manager._stop_runner(rid)
        manager._dp = None
        await engine.dispose()

    _aio.run(go())


def test_monthly_fee_active_only(tmp_path):
    async def body(s):
        # enabled + active bot + per-reseller fee → that fee
        r, _bot, _c = await _seed(s, tag="1", storefront_enabled=True, with_bot=True, fee=300_000)
        assert await storefront.monthly_fee_for(s, r) == 300_000

        # enabled flag but NO bot set up yet → no fee (active-only)
        r2, _b2, _c2 = await _seed(s, tag="2", storefront_enabled=True, with_bot=False)
        assert await storefront.monthly_fee_for(s, r2) == 0

        # feature disabled → no fee even if a bot row somehow exists
        r3, _b3, _c3 = await _seed(s, tag="3", storefront_enabled=False, with_bot=True, fee=300_000)
        assert await storefront.monthly_fee_for(s, r3) == 0

    _run(body, tmp_path, "fee.db")


# ── v1.44.0: atomic purchase, reaper, subscription lifecycle, retention ──────────

async def _seed_engine(tmp_path, name):  # noqa: ANN001, ANN202
    engine, Session = _engine_session(tmp_path, name)
    from app.core.db import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, Session


def test_purchase_is_atomic_and_refunds_on_provision_failure(tmp_path, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(usercreate, "create_for_reseller", _fake_create_factory(calls))

    async def go():
        engine, Session = await _seed_engine(tmp_path, "buy.db")
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            plan = await storefront.add_plan(s, bot.id, title="", gb=10, days=30, price_toman=50_000)
            await storefront_wallet.manual_adjust(s, cust, 80_000, note="seed")
            sf_id, cid, pid = bot.id, cust.id, plan.id

        res = await storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=cid, plan_id=pid, label="گوشی")
        assert res.ok and res.sub_link
        async with Session() as s:
            cust = await s.get(StorefrontCustomer, cid)
            assert int(storefront_wallet.balance(cust)) == 30_000      # 80k − 50k debited once
            orders = (await s.execute(
                StorefrontOrder.__table__.select().where(StorefrontOrder.customer_id == cid)
            )).all()
            assert len(orders) == 1
            order = await s.get(StorefrontOrder, orders[0].id)
            assert order.status == "provisioned" and order.label == "گوشی"
            # the order's pre-generated uuid IS the panel user's uuid (embedded in the sub-link)
            assert order.panel_user_uuid and order.panel_user_uuid in res.sub_link
            txn = (await s.execute(
                StorefrontWalletTxn.__table__.select().where(
                    StorefrontWalletTxn.kind == "purchase")
            )).first()
            assert txn is not None and txn.order_id == order.id   # money linked to its order
        await engine.dispose()

    asyncio.run(go())


def test_purchase_refunds_when_provision_fails(tmp_path, monkeypatch):
    async def fail_create(session, reseller, *, count, gb, days, base_name, user_uuid=None):  # noqa: ANN001, ANN002
        return SimpleNamespace(created=[], error=None, capacity_blocked=True, limit_hit=False)
    monkeypatch.setattr(usercreate, "create_for_reseller", fail_create)

    async def go():
        engine, Session = await _seed_engine(tmp_path, "buyfail.db")
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            plan = await storefront.add_plan(s, bot.id, title="", gb=10, days=30, price_toman=50_000)
            await storefront_wallet.manual_adjust(s, cust, 80_000, note="seed")
            sf_id, cid, pid = bot.id, cust.id, plan.id
        res = await storefront_provision.purchase(
            Session, sf_id=sf_id, customer_id=cid, plan_id=pid, label="x")
        assert res.ok is False and res.reason == "capacity"
        async with Session() as s:
            cust = await s.get(StorefrontCustomer, cid)
            assert int(storefront_wallet.balance(cust)) == 80_000   # fully refunded
            order = (await s.execute(
                StorefrontOrder.__table__.select().where(StorefrontOrder.customer_id == cid)
            )).first()
            assert order.status == "failed"
        await engine.dispose()

    asyncio.run(go())


def test_reaper_completes_existing_and_refunds_missing(tmp_path, monkeypatch):
    import datetime as _dt

    from app.services.panel_client import admin_api

    async def fake_get_user(self, panel, uuid, *, api_key=None):  # noqa: ANN001, ANN002
        return {"current_usage_GB": 0} if uuid == "exists" else None

    async def noop_notify(sf, customer, order):  # noqa: ANN001
        return None

    monkeypatch.setattr(admin_api.AdminApiClient, "get_user", fake_get_user)
    monkeypatch.setattr(storefront_provision, "_notify_completed", noop_notify)

    async def go():
        engine, Session = await _seed_engine(tmp_path, "reaper.db")
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            a = StorefrontOrder(customer_id=cust.id, panel_id=bot.panel_id, label="A", gb=10,
                                days=30, price_toman=50_000, status="pending", panel_user_uuid="exists")
            b = StorefrontOrder(customer_id=cust.id, panel_id=bot.panel_id, label="B", gb=10,
                                days=30, price_toman=50_000, status="pending", panel_user_uuid="gone")
            s.add_all([a, b])
            await s.flush()
            s.add(StorefrontWalletTxn(customer_id=cust.id, kind="purchase", amount_toman=-50_000,
                                      status="done", order_id=b.id))
            await s.commit()
            aid, bid, cid = a.id, b.id, cust.id

        async with Session() as s:
            res = await storefront_provision.reap_pending_orders(
                s, older_than=_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=1))
        assert res["completed"] == 1 and res["refunded"] == 1
        async with Session() as s:
            assert (await s.get(StorefrontOrder, aid)).status == "provisioned"
            assert (await s.get(StorefrontOrder, bid)).status == "failed"
            cust = await s.get(StorefrontCustomer, cid)
            assert int(storefront_wallet.balance(cust)) == 50_000   # B refunded once
        await engine.dispose()

    asyncio.run(go())


def test_renew_charges_current_price_in_place(tmp_path, monkeypatch):
    from app.services.panel_client import admin_api

    seen = {}

    async def fake_renew_user(self, panel, uuid, *, gb, days, api_key=None):  # noqa: ANN001, ANN002
        seen.update(uuid=uuid, gb=gb, days=days)

    monkeypatch.setattr(admin_api.AdminApiClient, "renew_user", fake_renew_user)

    async def go():
        engine, Session = await _seed_engine(tmp_path, "renew.db")
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            plan = await storefront.add_plan(s, bot.id, title="", gb=10, days=30, price_toman=50_000)
            await storefront_wallet.manual_adjust(s, cust, 80_000, note="seed")
            order = StorefrontOrder(customer_id=cust.id, plan_id=plan.id, panel_id=bot.panel_id,
                                    label="x", gb=10, days=30, price_toman=50_000,
                                    status="provisioned", panel_user_uuid="u1",
                                    sub_link="https://h/p/u1/#x")
            s.add(order)
            await s.flush()
            plan.price_toman = 60_000   # price went UP after purchase
            await s.commit()
            oid, cid = order.id, cust.id

        res = await storefront_subscription.renew(Session, order_id=oid, by_admin=False)
        assert res.ok and res.price == 60_000 and seen["uuid"] == "u1"
        async with Session() as s:
            cust = await s.get(StorefrontCustomer, cid)
            assert int(storefront_wallet.balance(cust)) == 20_000   # charged the NEW 60k
            order = await s.get(StorefrontOrder, oid)
            assert order.sub_link == "https://h/p/u1/#x" and order.last_renewed_at is not None
        await engine.dispose()

    asyncio.run(go())


def test_delete_subscription_removes_panel_user(tmp_path, monkeypatch):
    from app.services.panel_client import admin_api

    seen = {}

    async def fake_delete_user(self, panel, uuid, *, api_key=None):  # noqa: ANN001, ANN002
        seen["uuid"] = uuid

    monkeypatch.setattr(admin_api.AdminApiClient, "delete_user", fake_delete_user)

    async def go():
        engine, Session = await _seed_engine(tmp_path, "del.db")
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            order = StorefrontOrder(customer_id=cust.id, panel_id=bot.panel_id, label="x", gb=10,
                                    days=30, price_toman=0, status="provisioned", panel_user_uuid="u1")
            s.add(order)
            await s.commit()
            oid = order.id
        res = await storefront_subscription.delete_subscription(Session, order_id=oid)
        assert res.ok and seen["uuid"] == "u1"
        async with Session() as s:
            assert (await s.get(StorefrontOrder, oid)).status == "deleted"
        await engine.dispose()

    asyncio.run(go())


def test_renew_user_grants_fresh_quota_on_top_of_usage(tmp_path, monkeypatch):
    from app.services.panel_client.admin_api import AdminApiClient

    body = {}

    async def fake_get_user(self, panel, uuid, *, api_key=None):  # noqa: ANN001, ANN002
        return {"current_usage_GB": 7.5}

    async def fake_patch_user(self, panel, uuid, b, *, api_key=None):  # noqa: ANN001, ANN002
        body.update(b)

    monkeypatch.setattr(AdminApiClient, "get_user", fake_get_user)
    monkeypatch.setattr(AdminApiClient, "patch_user", fake_patch_user)

    async def go():
        await AdminApiClient().renew_user(SimpleNamespace(), "u1", gb=10, days=30, api_key="k")
        assert body["usage_limit_GB"] == 17.5   # used 7.5 + fresh 10 → remaining is exactly 10
        assert body["package_days"] == 30 and body["start_date"] is None and body["enable"] is True

    asyncio.run(go())


def test_retention_purges_tire_kickers_keeps_ledger(tmp_path):
    import datetime as _dt

    old = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=200)

    async def go():
        engine, Session = await _seed_engine(tmp_path, "ret.db")
        async with Session() as s:
            _r, bot, _c = await _seed(s)
            # A: pure tire-kicker (no orders, zero balance) → purged
            a = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=1001, last_seen_at=old)
            # B: has a provisioned order → kept
            b = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=1002, last_seen_at=old)
            # C: has a confirmed top-up → kept
            c = StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=1003, last_seen_at=old)
            s.add_all([a, b, c])
            await s.flush()
            s.add(StorefrontOrder(customer_id=b.id, panel_id=bot.panel_id, label="x", gb=10, days=30,
                                  price_toman=0, status="provisioned", panel_user_uuid="u1"))
            s.add(StorefrontWalletTxn(customer_id=c.id, kind="topup", amount_toman=100_000,
                                      status="confirmed"))
            # junk for B that should be swept (failed order, old)
            junk = StorefrontOrder(customer_id=b.id, panel_id=bot.panel_id, label="j", gb=1, days=1,
                                   price_toman=0, status="failed", panel_user_uuid="z")
            s.add(junk)
            await s.flush()
            # backdate the failed order so it's older than the window
            junk.created_at = old
            await s.commit()
            aid, bid, cid = a.id, b.id, c.id

        async with Session() as s:
            counts = await maintenance.prune_stale_storefront(s)
        assert counts["customers"] == 1
        async with Session() as s:
            assert await s.get(StorefrontCustomer, aid) is None       # tire-kicker purged
            assert await s.get(StorefrontCustomer, bid) is not None    # kept (provisioned order)
            assert await s.get(StorefrontCustomer, cid) is not None    # kept (confirmed top-up)
            kept_topup = (await s.execute(
                StorefrontWalletTxn.__table__.select().where(StorefrontWalletTxn.customer_id == cid)
            )).all()
            assert len(kept_topup) == 1   # the ledger top-up survives
        await engine.dispose()

    asyncio.run(go())


def test_banned_customer_is_blocked_everywhere(tmp_path, monkeypatch):
    from app.bot.storefront import handlers as sfh

    async def go():
        engine, Session = await _seed_engine(tmp_path, "ban.db")
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            cust.banned = True
            await s.commit()
            tgid, banned_tg = bot.bot_telegram_id, cust.telegram_id
        monkeypatch.setattr(sfh, "SessionLocal", Session)
        assert await sfh._is_banned(
            SimpleNamespace(id=tgid),
            SimpleNamespace(id=banned_tg, first_name="C", username="c")) is True
        # a different, non-banned customer is allowed through
        assert await sfh._is_banned(
            SimpleNamespace(id=tgid),
            SimpleNamespace(id=424242, first_name="X", username="x")) is False
        await engine.dispose()

    asyncio.run(go())


def test_bot_telegram_id_is_partial_unique(tmp_path):
    from sqlalchemy.exc import IntegrityError

    async def go():
        engine, Session = await _seed_engine(tmp_path, "uq.db")
        async with Session() as s:
            p1 = Panel(key="u1", host="u1.invalid", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
            p2 = Panel(key="u2", host="u2.invalid", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
            s.add_all([p1, p2])
            await s.flush()
            r1 = Reseller(panel_id=p1.id, admin_uuid="U1", name="R1")
            r2 = Reseller(panel_id=p2.id, admin_uuid="U2", name="R2")
            s.add_all([r1, r2])
            await s.flush()
            # two NULL bot_telegram_id rows are allowed (partial index excludes NULLs)
            s.add_all([
                StorefrontBot(reseller_id=r1.id, panel_id=p1.id, bot_token_enc="a"),
                StorefrontBot(reseller_id=r2.id, panel_id=p2.id, bot_token_enc="b"),
            ])
            await s.commit()
            # but two rows with the SAME non-null id collide
            await s.execute(
                StorefrontBot.__table__.update()
                .where(StorefrontBot.reseller_id == r1.id).values(bot_telegram_id=777))
            await s.commit()
            with pytest.raises(IntegrityError):
                await s.execute(
                    StorefrontBot.__table__.update()
                    .where(StorefrontBot.reseller_id == r2.id).values(bot_telegram_id=777))
                await s.commit()
        await engine.dispose()

    asyncio.run(go())


# ── v1.45.0: storefront enable default-on, one-bot-per-person, sub-reseller gate ──────

def test_reseller_storefront_enabled_defaults_on(tmp_path):
    async def body(s):
        p = Panel(key="d", host="d.invalid", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
        s.add(p)
        await s.flush()
        r = Reseller(panel_id=p.id, admin_uuid="X", name="X")   # storefront_enabled NOT passed
        s.add(r)
        await s.flush()
        await s.refresh(r)
        assert r.storefront_enabled is True   # default ON for everyone

    _run(body, tmp_path, "sfdefault.db")


def test_two_panels_get_separate_bots(tmp_path):
    async def body(s):
        # one person (same bot_chat_id) top-level on two panels → a SEPARATE bot per panel
        rids = []
        for i, tok in enumerate(["111:aaa", "222:bbb"], start=1):
            p = Panel(key=f"tp{i}", host=f"tp{i}.invalid",
                      proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
            s.add(p)
            await s.flush()
            r = Reseller(panel_id=p.id, admin_uuid=f"P{i}", name=f"P{i}",
                         bot_chat_id=555, storefront_enabled=True)
            s.add(r)
            await s.flush()
            await storefront.upsert_bot(s, reseller_id=r.id, panel_id=p.id, token=tok,
                                        bot_username=f"b{i}", bot_telegram_id=1000 + i)
            rids.append(r.id)
        a = await storefront.get_bot_for_reseller(s, rids[0])
        b = await storefront.get_bot_for_reseller(s, rids[1])
        assert a is not None and b is not None and a.id != b.id    # two distinct bots, one per panel
        assert {a.bot_telegram_id, b.bot_telegram_id} == {1001, 1002}

    _run(body, tmp_path, "twopanels.db")


def test_upsert_bot_repoint_migrates_data_no_duplicate(tmp_path):
    async def body(s):
        r, bot, cust = await _seed(s)
        plan = await storefront.add_plan(s, bot.id, title="", gb=10, days=30, price_toman=1000)
        old_id = bot.id
        updated = await storefront.upsert_bot(
            s, reseller_id=r.id, panel_id=r.panel_id, token="999:newtoken",
            bot_username="newbot", bot_telegram_id=8888)
        assert updated.id == old_id                               # SAME row repointed
        assert updated.bot_telegram_id == 8888 and updated.bot_username == "newbot"
        assert storefront.bot_token(updated) == "999:newtoken"    # token migrated
        plans = await storefront.list_plans(s, bot.id)
        custs = await storefront.list_customers(s, bot.id)
        assert len(plans) == 1 and plans[0].id == plan.id         # data preserved
        assert any(c.id == cust.id for c in custs)
        rows = (await s.execute(
            StorefrontBot.__table__.select().where(StorefrontBot.reseller_id == r.id))).all()
        assert len(rows) == 1                                     # never a second bot

    _run(body, tmp_path, "repoint.db")


def test_subreseller_cannot_setup_storefront(tmp_path):
    from app.bot import handlers as h

    async def body(s):
        p = Panel(key="h", host="h.invalid", proxy_path_enc=crypto.encrypt("x"), owner_uuid="o")
        s.add(p)
        await s.flush()
        s.add_all([
            Reseller(panel_id=p.id, admin_uuid="OWNER", name="Owner", is_owner=True),
            Reseller(panel_id=p.id, admin_uuid="TL", parent_admin_uuid="OWNER", name="TL",
                     bot_chat_id=111, storefront_enabled=True),
            Reseller(panel_id=p.id, admin_uuid="SUB", parent_admin_uuid="TL", name="SUB",
                     bot_chat_id=222, storefront_enabled=True),
        ])
        await s.commit()
        assert await h._can_setup_storefront(s, 111) is True    # first-tier admin → allowed
        assert await h._can_setup_storefront(s, 222) is False   # their sub-reseller → blocked
        assert await h._top_level_resellers(s, 222) == []

    _run(body, tmp_path, "subgate.db")


def test_my_services_detail_sends_single_message(tmp_path, monkeypatch):
    # Tapping a service in «سرویس‌های من» must send exactly ONE message (the QR photo with the status +
    # link + action buttons), not a detail message AND a separate «… آماده شد» config message.
    from app.bot.storefront import handlers as sfh
    from app.services import storefront_provision as sp

    async def go():
        engine, Session = await _seed_engine(tmp_path, "detail.db")
        async with Session() as s:
            _r, bot, cust = await _seed(s)
            order = StorefrontOrder(customer_id=cust.id, panel_id=bot.panel_id, label="x", gb=10,
                                    days=30, price_toman=1000, status="provisioned",
                                    panel_user_uuid="u1", sub_link="https://h/p/u1/#x")
            s.add(order)
            await s.commit()
            oid, cust_tg, bot_tg = order.id, cust.telegram_id, bot.bot_telegram_id

        monkeypatch.setattr(sfh, "SessionLocal", Session)
        monkeypatch.setattr(sfh.usercreate, "qr_png", lambda link: b"PNG")

        async def fake_live(session, sf, o):  # noqa: ANN001
            return sp.LiveStatus(True, used_gb=1.0, limit_gb=10.0, remaining_days=20)

        monkeypatch.setattr(sfh.storefront_provision, "live_status", fake_live)

        sends = {"n": 0}

        class FakeBot:
            id = bot_tg

            async def send_photo(self, *a, **k):  # noqa: ANN002, ANN003
                sends["n"] += 1

            async def send_message(self, *a, **k):  # noqa: ANN002, ANN003
                sends["n"] += 1

        class FakeMsg:
            async def answer(self, *a, **k):  # noqa: ANN002, ANN003
                sends["n"] += 1

        class FakeCb:
            data = f"sforder:{oid}"
            from_user = SimpleNamespace(id=cust_tg, first_name="C", username="c")
            message = FakeMsg()

            async def answer(self, *a, **k):  # noqa: ANN002, ANN003
                return None

        await sfh.sf_order_detail(FakeCb(), FakeBot())
        assert sends["n"] == 1   # exactly one outbound message
        await engine.dispose()

    asyncio.run(go())


# ── v1.46.0: storefront forced-join (channel membership) ─────────────────────

def test_join_keyboards_callbacks():
    on = SimpleNamespace(channel_required=True, channel_id="-100x")
    cbs = [b.callback_data for row in sfkb.join_settings_kb(on).inline_keyboard for b in row]
    assert {"sfjointog", "sfjoinset", "sfjoinclear"} <= set(cbs)
    off = SimpleNamespace(channel_required=False, channel_id=None)
    cbs2 = [b.callback_data for row in sfkb.join_settings_kb(off).inline_keyboard for b in row]
    assert "sfjoinclear" not in cbs2          # no clear button when no channel is set
    pk = [b.callback_data for row in sfkb.join_prompt_kb("https://t.me/x").inline_keyboard
          for b in row if b.callback_data]
    assert "sfjoincheck" in pk


def test_channel_block_gates_non_member_customer(tmp_path, monkeypatch):
    from app.bot.storefront import handlers as sfh

    async def go():
        engine, Session = await _seed_engine(tmp_path, "chanblock.db")
        async with Session() as s:
            r, bot, cust = await _seed(s)
            bot.channel_id, bot.channel_required, bot.channel_link = "-1009999", True, "https://t.me/x"
            r.bot_chat_id = 4242                # the admin's telegram id (customer is 555)
            await s.commit()
            rid, bot_tg, cust_tg = r.id, bot.bot_telegram_id, cust.telegram_id

        monkeypatch.setattr(sfh, "SessionLocal", Session)
        status = {"v": "left"}

        class FakeBot:
            id = bot_tg

            async def get_chat_member(self, chat, uid):  # noqa: ANN001
                return SimpleNamespace(status=status["v"], is_member=False)

        cust_user = SimpleNamespace(id=cust_tg, first_name="C", username="c")
        admin_user = SimpleNamespace(id=4242, first_name="A", username="a")
        fb = FakeBot()

        assert (await sfh._channel_block(fb, cust_user)) is not None      # non-member → blocked
        status["v"] = "member"
        assert (await sfh._channel_block(fb, cust_user)) is None          # member → allowed
        status["v"] = "left"
        assert (await sfh._channel_block(fb, admin_user)) is None         # admin → never gated
        async with Session() as s:                                       # required OFF → allowed
            b = await storefront.get_bot_for_reseller(s, rid)
            b.channel_required = False
            await s.commit()
        assert (await sfh._channel_block(fb, cust_user)) is None
        await engine.dispose()

    asyncio.run(go())


def test_set_channel_requires_bot_admin(tmp_path, monkeypatch):
    from app.bot.storefront import handlers as sfh

    async def go():
        engine, Session = await _seed_engine(tmp_path, "setchan.db")
        async with Session() as s:
            r, bot, _c = await _seed(s)
            r.bot_chat_id = 4242
            await s.commit()
            rid, bot_tg = r.id, bot.bot_telegram_id

        monkeypatch.setattr(sfh, "SessionLocal", Session)
        admin_status = {"v": "left"}

        class FakeBot:
            id = bot_tg

            async def get_chat_member(self, chat, uid):  # noqa: ANN001
                return SimpleNamespace(status=admin_status["v"])

            async def get_chat(self, chat):  # noqa: ANN001
                return SimpleNamespace(username="mychan")

        class FakeState:
            async def clear(self):
                return None

            async def set_state(self, *a, **k):  # noqa: ANN002, ANN003
                return None

        class FakeMsg:
            from_user = SimpleNamespace(id=4242, first_name="A", username="a")
            text = None
            forward_origin = SimpleNamespace(chat=SimpleNamespace(type="channel", id=-1009999))

            async def answer(self, *a, **k):  # noqa: ANN002, ANN003
                return None

        # bot NOT admin → nothing saved, stays gated off
        await sfh.sf_join_channel_set(FakeMsg(), FakeState(), FakeBot())
        async with Session() as s:
            b = await storefront.get_bot_for_reseller(s, rid)
            assert b.channel_id is None and b.channel_required is False

        # bot IS admin → channel saved + forced-join enabled + link captured
        admin_status["v"] = "administrator"
        await sfh.sf_join_channel_set(FakeMsg(), FakeState(), FakeBot())
        async with Session() as s:
            b = await storefront.get_bot_for_reseller(s, rid)
            assert b.channel_id == "-1009999" and b.channel_required is True
            assert b.channel_link == "https://t.me/mychan"
        await engine.dispose()

    asyncio.run(go())


def test_join_toggle_requires_channel(tmp_path, monkeypatch):
    from app.bot.storefront import handlers as sfh

    async def go():
        engine, Session = await _seed_engine(tmp_path, "jointog.db")
        async with Session() as s:
            r, bot, _c = await _seed(s)
            r.bot_chat_id = 4242
            await s.commit()
            rid, bot_tg = r.id, bot.bot_telegram_id

        monkeypatch.setattr(sfh, "SessionLocal", Session)

        class FakeBot:
            id = bot_tg

            async def get_chat_member(self, chat, uid):  # noqa: ANN001
                return SimpleNamespace(status="administrator")

        class FakeCbMsg:
            async def edit_reply_markup(self, **k):  # noqa: ANN003
                return None

        class FakeCb:
            from_user = SimpleNamespace(id=4242, first_name="A", username="a")
            message = FakeCbMsg()

            async def answer(self, *a, **k):  # noqa: ANN002, ANN003
                return None

        await sfh.sf_join_toggle(FakeCb(), FakeBot())          # no channel → can't enable
        async with Session() as s:
            assert (await storefront.get_bot_for_reseller(s, rid)).channel_required is False
        async with Session() as s:                            # set a channel
            b = await storefront.get_bot_for_reseller(s, rid)
            b.channel_id = "-100777"
            await s.commit()
        await sfh.sf_join_toggle(FakeCb(), FakeBot())          # now it enables
        async with Session() as s:
            assert (await storefront.get_bot_for_reseller(s, rid)).channel_required is True
        await engine.dispose()

    asyncio.run(go())


# ── v1.47.0: paginated + searchable customers tab ────────────────────────────

def test_list_customers_page_and_search(tmp_path):
    async def body(s):
        _r, bot, _c = await _seed(s)   # seeds one customer (telegram 555, name "Cust")
        for i in range(2, 12):         # +10 → 11 total
            s.add(StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=1000 + i, name=f"name{i}"))
        await s.commit()
        rows, total = await storefront.list_customers_page(s, bot.id, offset=0, limit=8)
        assert total == 11 and len(rows) == 8
        rows2, total2 = await storefront.list_customers_page(s, bot.id, offset=8, limit=8)
        assert total2 == 11 and len(rows2) == 3                       # last page
        assert await storefront.count_customers(s, bot.id) == 11
        r_name, t_name = await storefront.list_customers_page(s, bot.id, query="name5")
        assert t_name == 1 and r_name[0].name == "name5"              # search by name substring
        r_id, t_id = await storefront.list_customers_page(s, bot.id, query="1005")
        assert t_id == 1 and r_id[0].telegram_id == 1005             # search by telegram id

    _run(body, tmp_path, "custpage.db")


def test_customers_page_kb_nav_and_search():
    rows = [SimpleNamespace(id=i, name=f"n{i}", telegram_id=i, wallet_balance_toman=0)
            for i in range(8)]
    cbs = [b.callback_data for row in sfkb.customers_page_kb(
        rows, page=0, per_page=8, total=20).inline_keyboard for b in row]
    assert any(c.startswith("sfcust:") for c in cbs)
    assert "sfcustpg:1" in cbs and "sfcustsearch" in cbs              # next + search, no prev on page 0
    assert "sfcustpg:-1" not in cbs
    cbs1 = [b.callback_data for row in sfkb.customers_page_kb(
        rows, page=1, per_page=8, total=20).inline_keyboard for b in row]
    assert "sfcustpg:0" in cbs1 and "sfcustpg:2" in cbs1             # prev + next on a middle page
    cbs2 = [b.callback_data for row in sfkb.customers_page_kb(
        rows, page=0, per_page=20, total=3, searching=True).inline_keyboard for b in row]
    assert "sfcustpg:0" in cbs2 and "sfcustsearch" not in cbs2        # search results → back, no search


def test_customer_detail_kb_actions():
    cbs = [b.callback_data for row in sfkb.customer_detail_kb(7).inline_keyboard for b in row]
    assert {"sfadj:7:+", "sfadj:7:-", "sfacust:7", "sfcustpg:0"} <= set(cbs)


def test_customers_tab_sends_single_message(tmp_path):
    from app.bot.storefront import handlers as sfh

    async def body(s):
        reseller, bot, _c = await _seed(s)
        for i in range(2, 12):
            s.add(StorefrontCustomer(storefront_bot_id=bot.id, telegram_id=2000 + i, name=f"c{i}"))
        await s.commit()
        sent = {"n": 0}

        class FakeMsg:
            async def answer(self, *a, **k):  # noqa: ANN002, ANN003
                sent["n"] += 1

        await sfh._admin_action("customers", FakeMsg(), None, s, bot, reseller)
        assert sent["n"] == 1   # ONE tidy message, not one-per-customer

    _run(body, tmp_path, "custonemsg.db")


def test_usage_line_flags_renewal():
    """P04: when the live limit exceeds the plan size (a renewal added quota), the usage line is
    labeled «شاملِ تمدید» so «۱۱ از ۲۰» over a 10 GB plan isn't confusing; otherwise it's plain."""
    from app.bot.storefront.handlers import _usage_line
    # renewed: limit 20 > plan 10 → flagged
    line = _usage_line(11.0, 20.0, 10)
    assert "شاملِ تمدید" in line and "11.00 از 20" in line
    # fresh: limit == plan → no flag
    assert "شاملِ تمدید" not in _usage_line(3.0, 10.0, 10)
    # tiny rounding slack doesn't trip the flag
    assert "شاملِ تمدید" not in _usage_line(0.0, 10.2, 10)


def test_sf_broadcast_bg_flood_control_and_summary():
    """N04: the shop broadcast fan-out uses the SHARED flood-control policy — a 429
    recipient is retried and DELIVERED in the same run (pre-N04 the inline handler loop
    swallowed the exception and silently dropped them), a blocked customer is counted
    and skipped without stopping the loop, and the admin chat gets a final summary with
    the admin keyboard."""
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
    from aiogram.methods import SendMessage

    from app.bot.storefront import handlers as H

    def _m():
        return SendMessage(chat_id=1, text="x")

    class FakeBot:
        def __init__(self):
            self.sent: list[tuple[int, str, object]] = []
            self._retried = False

        async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
            if chat_id == 1 and not self._retried:
                self._retried = True
                raise TelegramRetryAfter(method=_m(), message="flood", retry_after=0)
            if chat_id == 2:
                raise TelegramForbiddenError(method=_m(), message="blocked")
            self.sent.append((chat_id, text, reply_markup))

    async def go():
        bot = FakeBot()
        await H._sf_broadcast_bg(bot, 900, [1, 2, 3], "پیام ویژه", "همه")
        chat_ids = [c for c, _t, _k in bot.sent]
        # 1 → delivered after one 429; 2 → blocked (skipped, loop continues);
        # 3 → delivered; 900 → the admin summary, last.
        assert chat_ids == [1, 3, 900]
        summary_text, summary_kb = bot.sent[-1][1], bot.sent[-1][2]
        assert "🚫" in summary_text          # blocked count reported
        assert "❌" not in summary_text      # no hard failures
        assert "2" in summary_text           # sent == 2
        assert summary_kb is not None        # admin reply keyboard attached

    asyncio.run(go())
