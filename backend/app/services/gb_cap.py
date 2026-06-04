"""
Per-sub-reseller MONTHLY VOLUME (GB) cap — a quota Hiddify itself can't enforce (it
only caps user COUNT). A PARENT reseller sets a monthly sold-quota ceiling on a
sub-reseller from the bot; this module checks, after each sync, whether any capped sub
has reached its ceiling this billing month and sends a ONE-TIME heads-up to the parent
(and the sub). Informational only — no automatic suspension (the parent decides).

The alert is armed once per month via `Reseller.gb_cap_alerted_period`, so it fires once
when crossed and re-arms automatically when the month rolls over.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Reseller
from app.services import reseller_report
from app.services.periods import current_month

log = logging.getLogger("gb_cap")


async def _parent_of(session: AsyncSession, sub: Reseller) -> Reseller | None:
    if not sub.parent_admin_uuid:
        return None
    return (
        await session.execute(
            select(Reseller).where(
                Reseller.panel_id == sub.panel_id,
                Reseller.admin_uuid == sub.parent_admin_uuid,
            )
        )
    ).scalar_one_or_none()


async def check_caps(session: AsyncSession, *, bot: Bot | None = None) -> dict:
    """Scan every sub-reseller that has a gb_cap; alert (once/month) any that reached it.
    Returns a small summary. Never raises into the caller."""
    from app.bot.telegram import build_bot
    from app.services import notifier

    period = current_month().label
    counts = {"checked": 0, "over": 0, "alerted": 0}
    capped = (
        await session.execute(select(Reseller).where(Reseller.gb_cap.is_not(None), Reseller.gb_cap > 0))
    ).scalars().all()
    if not capped:
        return counts

    own_bot = False
    if bot is None:
        bot = await build_bot(session)
        own_bot = True
    try:
        for sub in capped:
            counts["checked"] += 1
            try:
                used = await reseller_report.current_billable_gb(session, sub)
            except Exception:  # noqa: BLE001
                continue
            cap = int(sub.gb_cap or 0)
            if cap <= 0 or used < cap:
                # Under cap: re-arm if a stale alert flag from a previous period lingers.
                if sub.gb_cap_alerted_period and sub.gb_cap_alerted_period != period:
                    sub.gb_cap_alerted_period = None
                continue
            counts["over"] += 1
            if sub.gb_cap_alerted_period == period:
                continue  # already warned this month
            parent = await _parent_of(session, sub)
            # Notify the parent (the one who set the cap and gets paid).
            if parent is not None and parent.bot_chat_id is not None:
                await notifier.send_to_reseller(
                    session, parent,
                    (f"📊 زیرمجموعهٔ شما «{sub.name}» در دورهٔ {period} به سقف حجمی رسید.\n"
                     f"سقف: {cap:g} گیگ | ساخته‌شده: {used:g} گیگ\n"
                     "در صورت نیاز می‌توانید سقف را افزایش دهید یا زیرمجموعه را مسدود کنید "
                     "(منوی «مدیریت زیرمجموعه‌ها»)."),
                    bot=bot,
                )
            # Also let the sub-reseller know, if they're on the bot.
            if sub.bot_chat_id is not None:
                await notifier.send_to_reseller(
                    session, sub,
                    (f"📊 شما در دورهٔ {period} به سقف حجمی تعیین‌شده ({cap:g} گیگ) رسیدید.\n"
                     "برای ادامه با نمایندهٔ بالادست خود هماهنگ کنید."),
                    bot=bot,
                )
            sub.gb_cap_alerted_period = period
            counts["alerted"] += 1
        await session.commit()
    finally:
        if own_bot and bot is not None:
            await bot.session.close()
    if counts["alerted"]:
        log.info("gb_cap check: %s", counts)
    return counts
