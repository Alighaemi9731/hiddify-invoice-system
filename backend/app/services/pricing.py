"""Toman → USDT conversion using the panel-configured rate."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import settings_service


def toman_to_usdt(amount_toman: float | Decimal, toman_per_usdt: float | int) -> Decimal:
    """Convert a Toman amount to USDT, rounded to 6 decimals (returns 0 if rate <= 0)."""
    rate = Decimal(str(toman_per_usdt or 0))
    if rate <= 0:
        return Decimal("0")
    usdt = Decimal(str(amount_toman)) / rate
    return usdt.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


async def get_rate(session: AsyncSession) -> int:
    """Current Toman-per-USDT rate from settings."""
    value = await settings_service.get(session, "toman_per_usdt", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def get_default_price_per_gb(session: AsyncSession) -> int:
    value = await settings_service.get(session, "default_price_per_gb", 1000)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1000


async def get_default_min_sale(session: AsyncSession) -> int:
    value = await settings_service.get(session, "min_sale_toman", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def get_excluded_usage_gb(session: AsyncSession) -> set[int]:
    """Package sizes (GB) treated as test configs and skipped (default {1})."""
    value = await settings_service.get(session, "excluded_usage_gb", [1])
    out: set[int] = set()
    if isinstance(value, (list, tuple)):
        for v in value:
            try:
                out.add(int(v))
            except (TypeError, ValueError):
                continue
    return out or {1}
