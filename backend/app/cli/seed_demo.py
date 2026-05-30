"""
Seed synthetic demo data (no real panel needed) so the app is explorable out of the box:

    python -m app.cli.seed_demo

Creates a 'demo' panel with an Owner, a few resellers (one with a sub-reseller, one
excluded), and end-users with assorted package sizes and creation dates spread across
the current and previous month. Safe to re-run (idempotent on the 'demo' panel).
"""
from __future__ import annotations

import asyncio
import datetime as dt

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import Panel
from app.models.enums import PanelStatus, SyncSource
from app.services import sync as sync_service
from app.services.bootstrap import run_bootstrap
from app.services.panel_client.base import PanelAdmin, PanelData, PanelUser

PACKAGES = [1, 5, 10, 20, 30, 50, 100]  # 1 GB is a test config (excluded)


def _build_data() -> PanelData:
    today = dt.date.today()
    first_this = today.replace(day=1)
    prev = first_this - dt.timedelta(days=1)

    admins = [
        PanelAdmin("demo-owner", "Owner", None, "super_admin", "-", None, 0, 0),
        PanelAdmin("demo-r1", "علی فروشنده", "demo-owner", "agent", "VIP", 111, 200, 200),
        PanelAdmin("demo-r1a", "زیرمجموعه علی", "demo-r1", "agent", "", None, 100, 100),
        PanelAdmin("demo-r2", "رضا موبایل", "demo-owner", "agent", "", 222, 150, 150),
        PanelAdmin("demo-r3", "مغازه داخلی", "demo-owner", "agent", "-", None, 50, 50),  # excluded
    ]

    users: list[PanelUser] = []
    plan = [
        ("demo-r1", 14), ("demo-r1a", 6), ("demo-r2", 10), ("demo-owner", 4), ("demo-r3", 5),
    ]
    n = 0
    for added_by, count in plan:
        for i in range(count):
            n += 1
            # alternate creation month between current and previous
            base = first_this if (i % 2 == 0) else prev.replace(day=1)
            day = min(1 + (i * 3) % 27, 27)
            start = base.replace(day=day)
            gb = PACKAGES[(n) % len(PACKAGES)]
            users.append(PanelUser(
                uuid=f"demo-u{n}", name=f"کاربر {n}", added_by_uuid=added_by,
                start_date=start, usage_limit_gb=float(gb),
                current_usage_gb=float(gb) * 0.4, package_days=30,
                enable=True, is_active=True, mode="no_reset", last_online=None, comment=None,
            ))
    return PanelData(admins=admins, users=users)


async def _run() -> None:
    await run_bootstrap()
    data = _build_data()
    async with SessionLocal() as session:
        panel = (await session.execute(select(Panel).where(Panel.key == "demo"))).scalar_one_or_none()
        if panel is None:
            panel = Panel(key="demo", name="پنل نمونه", host="demo.local",
                          owner_uuid="demo-owner", enabled=True,
                          source=SyncSource.sample, status=PanelStatus.ok)
            panel.proxy_path = "demo-offline"
            session.add(panel)
            await session.commit()
            await session.refresh(panel)
        run = await sync_service.sync_panel(session, panel, data=data, source=SyncSource.sample)
        print(f"Seeded demo panel: {run.admin_count} admins, {run.user_count} users.")
        print("Now generate invoices for the current or previous month from the panel "
              "(Dashboard → «صدور و ارسال ماهانه») or POST /api/invoices/generate.")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
