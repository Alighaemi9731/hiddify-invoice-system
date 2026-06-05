"""
Live USDT→Toman exchange rate.

The invoice is computed in Toman, then converted to USDT via this rate. The owner can either
set the rate **manually** (`rate_mode="manual"`, the historical behavior) or have it read
**automatically** (`rate_mode="auto"`) from a public Iranian-market API. The sources below quote
USDT directly in **Toman** (TMN — NOT Rial), so no ÷10. Nobitex is deliberately NOT used: its
API host (api.nobitex.ir) is geo-restricted and does not resolve from servers outside Iran;
Tetherland and Wallex both resolve and respond internationally (verified from the prod host).

`get_effective_rate` is the single source of truth used by pricing/invoicing. It NEVER does
network I/O — it reads the last-fetched value cached in settings. The value is refreshed by a
scheduler job, by the «به‌روزرسانی نرخ» button, and best-effort right before invoice generation.
A failed fetch never breaks anything: auto mode falls back to the last good auto rate, and then
to the manual rate.
"""
from __future__ import annotations

import datetime as dt
import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import settings_service

log = logging.getLogger("rates")

# Both quote USDT in Toman (TMN) directly. Primary: Tetherland; fallback: Wallex.
_TETHERLAND = "https://api.tetherland.com/currencies"
_WALLEX = "https://api.wallex.ir/v1/markets"


def _pos_int(value) -> int | None:
    """Coerce a numeric-ish value to a positive int, else None."""
    try:
        v = int(round(float(value)))
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


async def fetch_usdt_toman() -> int | None:
    """Fetch the current USDT price in **Toman** from Tetherland (fallback Wallex). Returns None
    on any failure (network, parse, non-positive) — the caller keeps the previous value."""
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "invoice-system/1"}) as client:
        # Primary: Tetherland — data.currencies.USDT.price is already in Toman.
        try:
            r = await client.get(_TETHERLAND)
            r.raise_for_status()
            usdt = (((r.json().get("data") or {}).get("currencies") or {}).get("USDT") or {})
            rate = _pos_int(usdt.get("price"))
            if rate:
                return rate
        except Exception:  # noqa: BLE001 — fall through to Wallex
            log.info("tetherland rate fetch failed, trying wallex", exc_info=True)
        # Fallback: Wallex — result.symbols.USDTTMN.stats.bidPrice (Toman).
        try:
            r = await client.get(_WALLEX)
            r.raise_for_status()
            symbols = ((r.json().get("result") or {}).get("symbols") or {})
            stats = (symbols.get("USDTTMN") or {}).get("stats") or {}
            rate = _pos_int(stats.get("bidPrice"))
            if rate:
                return rate
        except Exception:  # noqa: BLE001
            log.warning("failed to fetch USDT→Toman rate (tetherland + wallex)", exc_info=True)
    return None


# Plausibility guard. An absolute band catches absurd values; the relative band catches a
# units change (e.g. a source silently switching Toman→Rial = 10×) by rejecting anything wildly
# off the previous known rate. USDT↔Toman never moves >3× hour-over-hour, but the unit error is
# exactly 10×, so 3× cleanly separates them.
_ABS_MIN, _ABS_MAX = 1_000, 5_000_000


async def refresh_auto_rate(session: AsyncSession) -> int | None:
    """Fetch the live rate and cache it in settings (`toman_per_usdt_auto` + timestamp), but
    only if it's plausible. Returns the accepted rate, or None on failure / implausible value
    (the cached value is left untouched, so billing keeps the last good rate)."""
    rate = await fetch_usdt_toman()
    if not rate or rate < _ABS_MIN or rate > _ABS_MAX:
        if rate:
            log.warning("fetched rate %s outside absolute band — ignored", rate)
        return None
    prev = _pos_int(await settings_service.get(session, "toman_per_usdt_auto", 0)) \
        or _pos_int(await settings_service.get(session, "toman_per_usdt", 0))
    if prev and not (prev / 3 <= rate <= prev * 3):
        log.warning("fetched rate %s is >3× off the previous %s — ignored as implausible", rate, prev)
        return None
    await settings_service.set_value(session, "toman_per_usdt_auto", rate)
    await settings_service.set_value(
        session, "toman_per_usdt_auto_at",
        dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )
    log.info("USDT→Toman auto rate refreshed: %s", rate)
    return rate


async def get_effective_rate(session: AsyncSession) -> int:
    """The Toman-per-USDT rate to actually use (no network I/O).

    `auto` → the last-fetched live rate if we have one, else the manual rate as a safety net.
    `manual` → the owner-set rate.
    """
    cfg = await settings_service.get_many(
        session, ["rate_mode", "toman_per_usdt", "toman_per_usdt_auto"]
    )

    def _int(v) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    manual = _int(cfg.get("toman_per_usdt"))
    if str(cfg.get("rate_mode") or "manual").lower() == "auto":
        auto = _int(cfg.get("toman_per_usdt_auto"))
        return auto if auto > 0 else manual
    return manual
