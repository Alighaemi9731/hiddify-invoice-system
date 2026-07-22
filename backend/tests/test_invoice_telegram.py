"""The invoice list surfaces a reseller's Telegram chat id + @username so the panel can
render a clickable PV deep-link. A reseller who never started the bot has neither."""
import asyncio
import datetime as dt

from fastapi import Response
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.invoices import list_invoices
from app.core.db import Base
from app.models import BotUser, Invoice, Panel, Reseller
from app.models.enums import InvoiceStatus


def test_invoice_list_exposes_telegram_link(tmp_path):
    async def run() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'inv_tg.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as s:
                panel = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
                s.add(panel)
                await s.flush()

                connected = Reseller(
                    panel_id=panel.id, admin_uuid="a-connected", name="Connected",
                    bot_chat_id=5001,
                )
                lonely = Reseller(
                    panel_id=panel.id, admin_uuid="a-lonely", name="Lonely",
                )  # never started the bot
                s.add_all([connected, lonely])
                s.add(BotUser(telegram_id=5001, username="connected_admin"))
                await s.flush()

                def inv(r: Reseller) -> Invoice:
                    return Invoice(
                        reseller_id=r.id, panel_id=panel.id,
                        period_start=dt.date(2026, 6, 1), period_end=dt.date(2026, 6, 30),
                        period_label="2026-06", usage_gb=10, amount_toman=100_000,
                        status=InvoiceStatus.sent,
                    )

                s.add_all([inv(connected), inv(lonely)])
                await s.commit()

                rows = await list_invoices(
                Response(),
                    period="2026-06", sort="amount", order="desc", limit=200, offset=0,
                    session=s,
                )
                by_name = {r.reseller_name: r for r in rows}

                assert by_name["Connected"].reseller_chat_id == 5001
                assert by_name["Connected"].reseller_username == "connected_admin"
                # The 8-digit public number is present and never the raw row id.
                assert by_name["Connected"].number and by_name["Connected"].number != "1"

                assert by_name["Lonely"].reseller_chat_id is None
                assert by_name["Lonely"].reseller_username is None
        finally:
            await engine.dispose()

    asyncio.run(run())
