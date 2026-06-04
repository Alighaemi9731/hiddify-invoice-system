"""
Daily channel/group guard: remove people who started the bot but are NOT registered
resellers from the announcement channel AND the group (so randoms can't sit in a
private channel/group).

Safety: controlled by `channel_kick_enabled` (default False) — the SAME switch guards
both the channel and the group. When off it runs in DRY-RUN and only reports how many
WOULD be kicked. Admins/creators are never touched. Needs the bot to be an admin with
"ban users" permission in each chat.
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
    cfg = await settings_service.get_many(session, [
        "announcement_channel_id", "announcement_group_id", "channel_kick_enabled",
    ])
    chats = [(cid, label) for cid, label in (
        (str(cfg.get("announcement_channel_id") or ""), "channel"),
        (str(cfg.get("announcement_group_id") or ""), "group"),
    ) if cid]
    if not chats:
        return {"skipped": "no channel/group configured"}
    enabled = bool(cfg.get("channel_kick_enabled"))

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
            kicked_any = False
            for chat_id, _label in chats:
                try:
                    member = await bot.get_chat_member(chat_id, u.telegram_id)
                except Exception:  # noqa: BLE001 — not reachable / left already
                    continue
                if member.status in _PROTECTED:
                    continue
                if member.status in ("member", "restricted"):
                    counts["in_channel_non_reseller"] += 1
                    if enabled:
                        try:
                            await bot.ban_chat_member(chat_id, u.telegram_id)
                            await bot.unban_chat_member(chat_id, u.telegram_id)  # kick (allow rejoin)
                            kicked_any = True
                        except Exception:  # noqa: BLE001
                            log.warning("kick failed for %s in %s", u.telegram_id, chat_id, exc_info=True)
            if kicked_any:
                u.last_kicked_at = dt.datetime.now(dt.timezone.utc)
                counts["kicked"] += 1
        await session.commit()
    finally:
        await bot.session.close()
    log.info("Channel/group guard: %s", counts)
    return counts
