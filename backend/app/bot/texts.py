"""Render panel-editable message templates with safe placeholder substitution."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import settings_service


class _Safe(dict):
    def __missing__(self, key: str) -> str:  # leave unknown placeholders intact
        return "{" + key + "}"


async def render(session: AsyncSession, key: str, **kwargs) -> str:
    tpl = await settings_service.get(session, key, "") or ""
    try:
        return tpl.format_map(_Safe(**kwargs))
    except Exception:  # noqa: BLE001
        return tpl
