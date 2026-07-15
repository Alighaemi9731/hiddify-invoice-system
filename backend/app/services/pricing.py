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
    """Current Toman-per-USDT rate to use — the live auto rate when `rate_mode="auto"`,
    otherwise the manual setting (see `rates.get_effective_rate`)."""
    from app.services import rates

    return await rates.get_effective_rate(session)


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


async def get_max_snapshot_age_hours(session: AsyncSession) -> int:
    """Max age (hours) of a panel's last successful sync for it to still be billable (0 = no cap)."""
    value = await settings_service.get(session, "billing_max_snapshot_age_hours", 26)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 26


async def get_excluded_usage_gb(session: AsyncSession) -> set[float]:
    """Extra exact package sizes (GB) treated as test configs and skipped. Kept as floats so a
    configured decimal size (e.g. 1.5) is matched exactly rather than truncated to 1."""
    value = await settings_service.get(session, "excluded_usage_gb", [1])
    out: set[float] = set()
    if isinstance(value, (list, tuple)):
        for v in value:
            try:
                out.add(float(v))
            except (TypeError, ValueError):
                continue
    return out


async def get_free_threshold_gb(session: AsyncSession) -> float:
    """Configs with quota <= this many GB are free test configs (default 1 GB)."""
    value = await settings_service.get(session, "free_under_gb", 1)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 1.0


async def get_deleted_full_quota_over_gb(session: AsyncSession) -> float:
    """A user deleted from the panel that CONSUMED at least this many GB is billed its full SOLD
    quota (not just consumption). 0 disables — deleted users are billed on consumption only.
    Default 5 GB."""
    value = await settings_service.get(session, "deleted_full_quota_over_gb", 5)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 5.0
