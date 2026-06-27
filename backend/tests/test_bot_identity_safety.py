"""B06 bot identity, membership, HTML, and local-date regressions."""
import asyncio
import datetime as dt
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/bot-safety.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.bot.matching import parse_link  # noqa: E402
from app.models import Invoice, Panel, Reseller  # noqa: E402
from app.models.enums import EnforcementState, InvoiceStatus  # noqa: E402


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as session:
                await coro_fn(session)
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_registration_requires_unique_host_path_uuid(tmp_path):
    from app.bot import handlers
    from app.core import crypto

    uuid = "11111111-2222-3333-4444-555555555555"

    async def body(session):
        p1 = Panel(
            key="p1", host="Panel.Example.COM.", proxy_path_enc=crypto.encrypt("Secret/path"),
            owner_uuid="owner-1",
        )
        p2 = Panel(
            key="p2", host="panel.example.com", proxy_path_enc=crypto.encrypt("other"),
            owner_uuid="owner-2",
        )
        session.add_all([p1, p2])
        await session.flush()
        r1 = Reseller(panel_id=p1.id, admin_uuid=uuid.upper(), name="one")
        r2 = Reseller(panel_id=p2.id, admin_uuid=uuid, name="two")
        session.add_all([r1, r2])
        await session.commit()

        exact = parse_link(f"https://panel.example.com:443/Secret/path/{uuid}/#tag")
        assert await handlers._registration_candidate(session, exact) is r1

        wrong_path = parse_link(f"https://panel.example.com/wrong/{uuid}/")
        assert await handlers._registration_candidate(session, wrong_path) is None

        incomplete = parse_link(uuid)
        assert await handlers._registration_candidate(session, incomplete) is None

        # A second panel with the same normalized identity makes the result ambiguous.
        p3 = Panel(
            key="p3", host="PANEL.EXAMPLE.COM", proxy_path_enc=crypto.encrypt("/Secret/path/"),
            owner_uuid="owner-3",
        )
        session.add(p3)
        await session.flush()
        session.add(Reseller(panel_id=p3.id, admin_uuid=uuid, name="three"))
        await session.commit()
        assert await handlers._registration_candidate(session, exact) is None

    _run(body, tmp_path, "matching.db")


def test_message_membership_gate_blocks_commands_and_payment_state(monkeypatch):
    from app.bot import handlers

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(handlers, "SessionLocal", SessionContext)

    async def not_owner(session, user):
        return False

    async def missing(bot, session, user_id):
        return [{"label": "کانال"}]

    monkeypatch.setattr(handlers, "_is_owner_user", not_owner)
    monkeypatch.setattr(handlers, "_missing_gates", missing)

    async def run_case(text):
        calls = {"handler": 0, "answers": []}

        async def handler(event, data):
            calls["handler"] += 1

        async def answer(value):
            calls["answers"].append(value)

        event = SimpleNamespace(
            text=text,
            chat=SimpleNamespace(type="private"),
            from_user=SimpleNamespace(id=10),
            answer=answer,
        )
        await handlers._membership_gate_message_mw(handler, event, {"bot": object()})
        return calls

    direct = asyncio.run(run_case("/pay"))
    assert direct["handler"] == 0 and "عضو کانال" in direct["answers"][0]

    state_text = asyncio.run(run_case("0x" + "a" * 64))
    assert state_text["handler"] == 0

    state_photo = asyncio.run(run_case(None))
    assert state_photo["handler"] == 0

    assert asyncio.run(run_case("/start payload"))["handler"] == 1
    assert asyncio.run(run_case("/cancel"))["handler"] == 1


def test_support_html_escapes_user_content():
    from app.bot.handlers import _support_message_html

    user = SimpleNamespace(id=123, username=None, first_name="<b>A & B</b>")
    rendered = _support_message_html(user, "<a href='tg://user?id=9'>click</a> & text")
    assert "<b>A & B</b>" not in rendered
    assert "&lt;b&gt;A &amp; B&lt;/b&gt;" in rendered
    assert "<a href='tg://user?id=9'>" not in rendered
    assert "&lt;a href=&#x27;tg://user?id=9&#x27;&gt;" in rendered


def test_payable_revalidation_uses_tehran_today(tmp_path, monkeypatch):
    # The payable re-validation now lives in payments.submit_reseller_payment, which reads
    # the Tehran-local "today" from app.services.periods at call time.
    from app.services import payments

    local_today = dt.date(2026, 6, 10)
    monkeypatch.setattr("app.services.periods.today", lambda: local_today)

    async def body(session):
        reseller = Reseller(
            panel_id=1, admin_uuid="A", name="R",
            enforcement_state=EnforcementState.active,
        )
        session.add(reseller)
        await session.flush()
        invoice = Invoice(
            reseller_id=reseller.id, panel_id=1,
            period_start=dt.date(2026, 5, 1), period_end=dt.date(2026, 5, 31),
            period_label="2026-05", usage_gb=1, amount_toman=1, amount_usdt=1,
            status=InvoiceStatus.sent, deferred_until=local_today + dt.timedelta(days=1),
        )
        session.add(invoice)
        await session.commit()
        # Deferred to a FUTURE date → not payable yet.
        res = await payments.submit_reseller_payment(
            session, reseller_ids={reseller.id}, invoice_id=invoice.id, txid="0x" + "a" * 64)
        assert res.status == "not_payable"

        # Deadline reached today → payable.
        invoice.deferred_until = local_today
        await session.commit()
        res2 = await payments.submit_reseller_payment(
            session, reseller_ids={reseller.id}, invoice_id=invoice.id, txid="0x" + "b" * 64)
        assert res2.status == "ok"

    _run(body, tmp_path, "date.db")


def test_is_member_counts_restricted_but_still_in_group():
    """The forced-membership gate must treat a `restricted` (but is_member=True) supergroup member as a
    member — matching channel_guard. `left`/`kicked` and API errors stay non-member (fail closed)."""
    from app.bot import handlers

    class FakeBot:
        def __init__(self, member=None, raises=False):
            self._member, self._raises = member, raises

        async def get_chat_member(self, chat_id, user_id):  # noqa: ANN001
            if self._raises:
                raise RuntimeError("Bad Request: user not found")
            return self._member

    def member(status, **kw):
        return SimpleNamespace(status=status, **kw)

    async def check(bot) -> bool:
        return await handlers._is_member(bot, "-100123", 555)

    async def body():
        # restricted but still in the group → member
        assert await check(FakeBot(member("restricted", is_member=True))) is True
        # restricted and removed → non-member
        assert await check(FakeBot(member("restricted", is_member=False))) is False
        # plain statuses
        assert await check(FakeBot(member("member"))) is True
        assert await check(FakeBot(member("administrator"))) is True
        assert await check(FakeBot(member("creator"))) is True
        assert await check(FakeBot(member("left"))) is False
        assert await check(FakeBot(member("kicked"))) is False
        # API error → fail closed (non-member)
        assert await check(FakeBot(raises=True)) is False
        # no chat configured → gate is open (always "member")
        assert await handlers._is_member(FakeBot(), "", 555) is True

    asyncio.run(body())
