"""
Daily channel guard: remove people who started the bot but are NOT registered
resellers from the announcement channel (so randoms can't sit in a private channel).

Safety: controlled by `channel_kick_enabled` (default False). When off it runs in
DRY-RUN and only reports how many WOULD be kicked. Admins/creators are never touched.
Needs the bot to be a channel admin with "ban users" permission.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.telegram import build_bot
from app.models import BotUser, Reseller
from app.services import settings_service

log = logging.getLogger("channel_guard")

_PROTECTED = ("administrator", "creator", "owner")


async def enforce_channel(session: AsyncSession) -> dict:
    channel = await settings_service.get(session, "announcement_channel_id", "") or ""
    if not channel:
        return {"skipped": "no channel configured"}
    enabled = bool(await settings_service.get(session, "channel_kick_enabled", False))

    reseller_ids = {
        c for c in (
            await session.execute(
                select(Reseller.bot_chat_id).where(Reseller.bot_chat_id.is_not(None))
            )
        ).scalars().all() if c
    }
    users = (await session.execute(select(BotUser))).scalars().all()

    bot = await build_bot(session)
    counts = {"checked": 0, "in_channel_non_reseller": 0, "kicked": 0, "dry_run": not enabled}
    if bot is None:
        return {**counts, "error": "no bot token"}
    try:
        for u in users:
            if u.telegram_id in reseller_ids:
                continue  # a real reseller — leave them
            counts["checked"] += 1
            try:
                member = await bot.get_chat_member(channel, u.telegram_id)
            except Exception:  # noqa: BLE001 — not reachable / left already
                continue
            if member.status in _PROTECTED:
                continue
            if member.status in ("member", "restricted"):
                counts["in_channel_non_reseller"] += 1
                if enabled:
                    try:
                        await bot.ban_chat_member(channel, u.telegram_id)
                        await bot.unban_chat_member(channel, u.telegram_id)  # kick (allow rejoin)
                        u.last_kicked_at = dt.datetime.now(dt.timezone.utc)
                        counts["kicked"] += 1
                    except Exception:  # noqa: BLE001
                        log.warning("kick failed for %s", u.telegram_id, exc_info=True)
        await session.commit()
    finally:
        await bot.session.close()
    log.info("Channel guard: %s", counts)
    return counts
