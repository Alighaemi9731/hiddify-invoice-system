"""
One-off demo: build a sample invoice where one user is still on the panel (billed on SOLD
quota) and another was DELETED from the panel (billed on CONSUMPTION, labelled
« — مصرف حذف‌شده از پنل»), to show the v1.37.26/27 behaviour.

Runs against a throwaway SQLite DB (never touches prod data). Prints the rendered invoice
text + line items. With DEMO_SEND=1 it also sends the invoice to DEMO_CHAT_ID via the bot —
reading the token from the app's configured DB (app.core.db), so the token is never printed.

  # local (print only):
  PYTHONPATH=. .venv/bin/python scripts/demo_deleted_invoice.py
  # send to a chat (run where the prod bot token is configured):
  DEMO_SEND=1 DEMO_CHAT_ID=<ali_chat_id> PYTHONPATH=. python scripts/demo_deleted_invoice.py
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models import EndUserSnapshot, Invoice, Panel, Reseller
from app.models.enums import InvoiceStatus
from app.services import delivery, invoicing
from app.services.periods import current_month

OWNER_UUID = "demo-owner-uuid"
R_UUID = "demo-reseller-uuid"


async def seed(session: AsyncSession, now: dt.datetime, period_start: dt.date) -> Reseller:
    panel = Panel(
        key="demo", host="demo.local", proxy_path_enc="x", owner_uuid=OWNER_UUID,
        last_synced_at=now,
    )
    session.add(panel)
    await session.flush()

    # A sample payment method so the invoice's payment block isn't empty in the demo.
    from app.services import settings_service
    await settings_service.set_value(session, "pay_card_enabled", "true")
    await settings_service.set_value(session, "card_number", "6037-9900-0000-1234")
    await settings_service.set_value(session, "card_holder_name", "حساب نمونه")

    owner = Reseller(panel_id=panel.id, admin_uuid=OWNER_UUID, name="Owner", is_owner=True)
    r = Reseller(
        panel_id=panel.id, admin_uuid=R_UUID, name="نمایندهٔ نمونه",
        parent_admin_uuid=OWNER_UUID, bot_chat_id=int(os.environ.get("DEMO_CHAT_ID") or 0) or None,
    )
    session.add_all([owner, r])

    created = period_start + dt.timedelta(days=3)  # a creation date inside the billing month
    # Still on the panel → billed on the 30 GB it was SOLD.
    session.add(EndUserSnapshot(
        panel_id=panel.id, user_uuid="u-present", name="کاربر فعال",
        added_by_uuid=R_UUID, usage_limit_gb=30, current_usage_gb=8.0,
        start_date=created, enable=True, is_active=True, last_synced_at=now,
    ))
    # Removed from the panel (snapshot older than the panel's last sync) → billed on the 5 GB
    # it actually CONSUMED, not the 30 GB it was sold.
    session.add(EndUserSnapshot(
        panel_id=panel.id, user_uuid="u-deleted", name="کاربر حذف‌شده",
        added_by_uuid=R_UUID, usage_limit_gb=30, current_usage_gb=5.0,
        start_date=created, enable=True, is_active=True,
        last_synced_at=now - dt.timedelta(days=1),
    ))
    await session.commit()
    return r


async def main() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///./data/demo_inv.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    period = current_month()
    now = dt.datetime(period.start.year, period.start.month, 15, 12, 0)

    async with Session() as session:
        r = await seed(session, now, period.start)
        await invoicing.generate_invoices(session, period, force=True)

        inv = (await session.execute(
            Invoice.__table__.select().where(Invoice.reseller_id == r.id)
        )).first()
        invoice = await session.get(Invoice, inv.id)
        reseller = await session.get(Reseller, r.id)

        text = await delivery.build_invoice_text(session, invoice, reseller)
        from sqlalchemy import select
        from app.models import InvoiceLine
        lines = (await session.execute(
            select(InvoiceLine).where(InvoiceLine.invoice_id == invoice.id)
        )).scalars().all()

        print("=" * 60)
        print(f"INVOICE total_gb={invoice.usage_gb}  amount_toman={invoice.amount_toman:,}")
        print("LINES:")
        for ln in lines:
            print(f"  • {ln.name}  →  {ln.usage_gb} GB")
        print("=" * 60)
        print(text)
        print("=" * 60)

        if os.environ.get("DEMO_SEND") == "1":
            chat_id = int(os.environ["DEMO_CHAT_ID"])
            # Read the configured bot token from the app's real DB (NOT printed).
            from aiogram import Bot
            from app.core.db import async_session as prod_session
            from app.services import settings_service
            async with prod_session() as ps:
                token = await settings_service.get_value(ps, "telegram_bot_token")
            if not token:
                raise SystemExit("no telegram_bot_token configured")
            invoice.status = InvoiceStatus.sent  # so the «pay» button shows like a real send
            bot = Bot(token)
            try:
                ids = await delivery.send_invoice_content(session, bot, chat_id, invoice, reseller)
                print(f"SENT to {chat_id}: {len(ids)} message(s)")
            finally:
                await bot.session.close()

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
