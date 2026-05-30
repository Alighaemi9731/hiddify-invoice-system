"""
Send Telegram messages to resellers and record a DeliveryLog row.

Used by the bot AND by the scheduler/invoicing/dunning code, so delivery is logged
consistently regardless of who triggers it. A Bot can be passed in (reuse) or built
on demand from settings.
"""
from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.telegram import build_bot
from app.models import DeliveryLog, Reseller
from app.models.enums import DeliveryKind, DeliveryStatus

log = logging.getLogger("notifier")


async def send_to_reseller(
    session: AsyncSession,
    reseller: Reseller,
    text: str,
    *,
    kind: DeliveryKind = DeliveryKind.generic,
    invoice_id: int | None = None,
    bot: Bot | None = None,
) -> DeliveryLog:
    own_bot = False
    status = DeliveryStatus.sent
    error: str | None = None

    if reseller.bot_chat_id is None:
        status = DeliveryStatus.unmatched
        error = "reseller has not registered with the bot"
    else:
        if bot is None:
            bot = await build_bot(session)
            own_bot = True
        if bot is None:
            status = DeliveryStatus.failed
            error = "no telegram bot token configured"
        else:
            try:
                await bot.send_message(reseller.bot_chat_id, text)
            except TelegramForbiddenError:
                status = DeliveryStatus.blocked
                error = "reseller blocked the bot"
            except Exception as exc:  # noqa: BLE001
                status = DeliveryStatus.failed
                error = str(exc)[:500]
            finally:
                if own_bot and bot is not None:
                    await bot.session.close()

    entry = DeliveryLog(
        reseller_id=reseller.id, invoice_id=invoice_id, kind=kind,
        status=status, error=error, message_preview=text[:200],
    )
    session.add(entry)
    await session.commit()
    if status != DeliveryStatus.sent:
        log.warning("Delivery to reseller %s (%s): %s — %s",
                    reseller.id, reseller.name, status.value, error)
    return entry
