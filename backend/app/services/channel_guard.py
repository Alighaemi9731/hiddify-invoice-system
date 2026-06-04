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
        "kick_grace_minutes",
    ])
    chats = [(cid, label) for cid, label in (
        (str(cfg.get("announcement_channel_id") or ""), "channel"),
        (str(cfg.get("announcement_group_id") or ""), "group"),
    ) if cid]
    if not chats:
        return {"skipped": "no channel/group configured"}
    enabled = bool(cfg.get("channel_kick_enabled"))
    grace_minutes = float(cfg.get("kick_grace_minutes") or 0)
    now = dt.datetime.now(dt.timezone.utc)

    reseller_ids = {
        c for c in (
            await session.execute(
                select(Reseller.bot_chat_id).where(Reseller.bot_chat_id.is_not(None))
            )
        ).scalars().all() if c
    }
    users = (await session.execute(select(BotUser))).scalars().all()

    bot = await build_bot(session)
    counts = {"checked": 0, "in_channel_non_reseller": 0, "kicked": 0,
              "grace": 0, "dry_run": not enabled}
    if bot is None:
        return {**counts, "error": "no bot token"}
    try:
        for u in users:
            if u.telegram_id in reseller_ids:
                continue  # a real reseller — leave them
            # Grace period: don't kick someone who only just started the bot — give them a
            # short window to register their panel link first. With a 15-min grace and a
            # 10-min guard, a newcomer is skipped on the tick they joined near and removed
            # on the next one (the "second cycle").
            if grace_minutes > 0 and u.created_at is not None:
                created = u.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=dt.timezone.utc)
                if (now - created).total_seconds() < grace_minutes * 60:
                    counts["grace"] += 1
                    continue
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
                # Tell the removed user IN THE BOT why — they started the bot but never
                # registered their panel link, so they aren't a recognized reseller.
                try:
                    await bot.send_message(
                        u.telegram_id,
                        "⛔️ شما از کانال/گروه حذف شدید.\n"
                        "دلیل: هنوز لینک پنل خود را در ربات ثبت نکرده‌اید و به‌عنوان نمایندهٔ "
                        "معتبر شناخته نمی‌شوید.\n"
                        "اگر نمایندهٔ ما هستید، لطفاً لینک پنل خود را همین‌جا ارسال کنید تا ثبت "
                        "شوید و دوباره بتوانید عضو شوید.",
                    )
                    counts["notified"] = counts.get("notified", 0) + 1
                except Exception:  # noqa: BLE001 — user may have blocked the bot / never DMed it
                    log.info("kick notice to %s failed (blocked bot?)", u.telegram_id)
        await session.commit()
    finally:
        await bot.session.close()
    log.info("Channel/group guard: %s", counts)
    return counts
