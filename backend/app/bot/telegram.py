"""Helpers to build an aiogram Bot from the runtime settings."""
from __future__ import annotations

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import settings_service


async def get_token(session: AsyncSession) -> str | None:
    token = await settings_service.get(session, "telegram_bot_token")
    return token or None


async def build_bot(session: AsyncSession) -> Bot | None:
    """Create a Bot from the configured token (plain text mode), or None if unset."""
    token = await get_token(session)
    if not token:
        return None
    return Bot(token=token)
