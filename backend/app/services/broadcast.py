"""Send a message to all or a targeted group of bot-registered resellers."""
from __future__ import annotations

import logging

from aiogram.exceptions import TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.telegram import build_bot
from app.models import Invoice, Reseller
from app.models.enums import InvoiceStatus

log = logging.getLogger("broadcast")

_OWED = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)


async def _all_chat_ids(session: AsyncSession) -> set[int]:
    return {
        c for c in (
            await session.execute(
                select(Reseller.bot_chat_id).where(Reseller.bot_chat_id.is_not(None)).distinct()
            )
        ).scalars().all() if c
    }


async def _debtor_chat_ids(session: AsyncSession) -> set[int]:
    rows = (
        await session.execute(
            select(Reseller.bot_chat_id)
            .join(Invoice, Invoice.reseller_id == Reseller.id)
            .where(Reseller.bot_chat_id.is_not(None), Invoice.status.in_(_OWED))
            .distinct()
        )
    ).scalars().all()
    return {c for c in rows if c}


async def _panel_chat_ids(session: AsyncSession, panel_id: int) -> set[int]:
    rows = (
        await session.execute(
            select(Reseller.bot_chat_id).where(
                Reseller.bot_chat_id.is_not(None), Reseller.panel_id == panel_id
            ).distinct()
        )
    ).scalars().all()
    return {c for c in rows if c}


async def _registered_chat_ids(session: AsyncSession) -> set[int]:
    rows = (
        await session.execute(
            select(Reseller.bot_chat_id).where(Reseller.bot_chat_id.is_not(None)).distinct()
        )
    ).scalars().all()
    return {c for c in rows if c}


async def _zero_sale_chat_ids(session: AsyncSession) -> set[int]:
    """Registered resellers whose bundle sells nothing in the current month."""
    from app.services import invoicing
    from app.services.periods import current_month

    pairs = await invoicing.preview_bundles(session, current_month())
    return {
        b.root.bot_chat_id
        for _panel, b in pairs
        if b.total_gb <= 0 and b.root.bot_chat_id is not None
    }


async def resolve_audience(
    session: AsyncSession, audience: str = "all", panel_id: int | None = None
) -> set[int]:
    if audience == "debtors":
        return await _debtor_chat_ids(session)
    if audience == "zero_sale":
        return await _zero_sale_chat_ids(session)
    if audience == "panel" and panel_id is not None:
        return await _panel_chat_ids(session, panel_id)
    return await _all_chat_ids(session)


async def broadcast(
    session: AsyncSession, text: str, *, audience: str = "all", panel_id: int | None = None
) -> dict:
    """Send `text` to the chosen audience: all | debtors | zero_sale | panel(panel_id)."""
    chat_ids = sorted(await resolve_audience(session, audience, panel_id))
    counts = {"audience": audience, "total": len(chat_ids), "sent": 0, "failed": 0, "blocked": 0}
    if not chat_ids or not text.strip():
        return counts

    bot = await build_bot(session)
    if bot is None:
        counts["failed"] = len(chat_ids)
        return counts
    try:
        for cid in chat_ids:
            try:
                await bot.send_message(cid, text)
                counts["sent"] += 1
            except TelegramForbiddenError:
                counts["blocked"] += 1
            except Exception:  # noqa: BLE001
                counts["failed"] += 1
    finally:
        await bot.session.close()
    log.info("Broadcast: %s", counts)
    return counts
