"""Dunning day-counts anchor on the TEHRAN calendar day, not the UTC one.

Owner-reported: with reminder1_day=2 and invoices sent at Tehran 03:00 (= 23:30 UTC the
PREVIOUS day), the first reminder fired a day early — `sent_at.date()`/`now.date()`
extracted the UTC day, pulling the anchor back one day for any instant in the Tehran
00:00–03:29 window."""
from __future__ import annotations

import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/dun-tz.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import Invoice, Panel, Reseller  # noqa: E402
from app.models.enums import InvoiceStatus  # noqa: E402
from app.services import dunning, periods, settings_service  # noqa: E402

UTC = dt.timezone.utc


def test_to_local_date_maps_utc_evening_to_next_tehran_day():
    # 22:00 UTC = 01:30 Tehran (+3:30) the NEXT day.
    assert periods.to_local_date(dt.datetime(2026, 6, 30, 22, 0, tzinfo=UTC)) \
        == dt.date(2026, 7, 1)
    # Midday is the same calendar day in both zones.
    assert periods.to_local_date(dt.datetime(2026, 6, 30, 12, 0, tzinfo=UTC)) \
        == dt.date(2026, 6, 30)
    # Naive values are treated as UTC (storage convention).
    assert periods.to_local_date(dt.datetime(2026, 6, 30, 22, 0)) == dt.date(2026, 7, 1)


def test_reminder_not_fired_a_day_early(tmp_path):
    """sent_at = 2026-06-30 22:00 UTC (Tehran: 07-01). With reminder1_day=2:
    on Tehran 07-02 (1 Tehran day elapsed; 2 UTC days — the old bug) nothing may fire;
    on Tehran 07-03 the reminder fires."""
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'dtz.db'}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                panel = Panel(key="p", host="h", proxy_path_enc="x", owner_uuid="o")
                s.add(panel)
                await s.flush()
                r = Reseller(panel_id=panel.id, admin_uuid="a", name="R", bot_chat_id=1)
                s.add(r)
                await s.flush()
                inv = Invoice(
                    reseller_id=r.id, panel_id=panel.id,
                    period_start=dt.date(2026, 6, 1), period_end=dt.date(2026, 6, 30),
                    period_label="2026-06", usage_gb=10, amount_toman=100_000,
                    amount_usdt=1, status=InvoiceStatus.sent,
                    sent_at=dt.datetime(2026, 6, 30, 22, 0, tzinfo=UTC),  # Tehran 07-01 01:30
                )
                s.add(inv)
                # Pin the reminder day this test's Tehran-vs-UTC arithmetic is written
                # against, independent of the shipped default.
                await settings_service.set_value(s, "reminder1_day", 2)
                await s.commit()

                # Tehran 2026-07-02 15:30 → 1 Tehran day since sent. The pre-fix UTC
                # anchor made this look like day 2 and fired the reminder early.
                res1 = await dunning.run_dunning(
                    s, now=dt.datetime(2026, 7, 2, 12, 0, tzinfo=UTC))
                assert res1["reminder1"] == 0, "reminder fired a day early (UTC anchor)"

                # Tehran 2026-07-03 → day 2 → now it fires (attempted; no bot token here).
                res2 = await dunning.run_dunning(
                    s, now=dt.datetime(2026, 7, 3, 12, 0, tzinfo=UTC))
                assert res2["reminder1"] == 1
        finally:
            await engine.dispose()

    asyncio.run(go())
