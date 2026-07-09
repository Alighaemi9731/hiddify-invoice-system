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
from aiogram.types import FSInputFile
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
            from app.bot.rtl import rtl

            try:
                await bot.send_message(reseller.bot_chat_id, rtl(text))
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


async def send_document_to_reseller(
    session: AsyncSession,
    reseller: Reseller,
    path: str,
    *,
    caption: str = "",
    bot: Bot | None = None,
) -> bool:
    """Best-effort: send a reseller a document (e.g. a payment-receipt PDF). Returns True on send.
    Not logged as a DeliveryLog row (the accompanying text ack already is) and never raises."""
    if reseller.bot_chat_id is None:
        return False
    own_bot = bot is None
    if bot is None:
        bot = await build_bot(session)
    if bot is None:
        return False
    from app.bot.rtl import rtl
    try:
        await bot.send_document(
            reseller.bot_chat_id, FSInputFile(path),
            caption=rtl(caption) if caption else None,
        )
        return True
    except TelegramForbiddenError:
        return False
    except Exception:  # noqa: BLE001 — supplementary to the text ack; never break the caller
        log.warning("document send to reseller %s failed", reseller.id, exc_info=True)
        return False
    finally:
        if own_bot and bot is not None:
            await bot.session.close()
