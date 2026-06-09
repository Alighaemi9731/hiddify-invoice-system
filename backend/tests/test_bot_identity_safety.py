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
    from app.bot import handlers

    local_today = dt.date(2026, 6, 10)
    monkeypatch.setattr(handlers, "tehran_today", lambda: local_today)

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
        assert await handlers._revalidate_payable(session, invoice, {reseller.id}) is None

        invoice.deferred_until = local_today
        await session.commit()
        assert await handlers._revalidate_payable(session, invoice, {reseller.id}) is not None

    _run(body, tmp_path, "date.db")
