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


async def fetch_ton_toman() -> int | None:
    """Fetch the current TON (Toncoin) price in **Toman** from Wallex (TONTMN market). Tetherland
    has no TON, so Wallex is the only source. Display-only (the TON amount shown to the customer);
    a failure just hides the amount. Returns None on failure."""
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "invoice-system/1"}) as client:
            r = await client.get(_WALLEX)
            r.raise_for_status()
            symbols = ((r.json().get("result") or {}).get("symbols") or {})
            stats = (symbols.get("TONTMN") or {}).get("stats") or {}
            return _pos_int(stats.get("bidPrice"))
    except Exception:  # noqa: BLE001
        log.warning("failed to fetch TON→Toman rate from Wallex", exc_info=True)
    return None


# TON trades far higher than USDT in Toman (a few hundred k–1M+); generous absolute band.
_TON_MIN, _TON_MAX = 50_000, 5_000_000


async def refresh_ton_rate(session: AsyncSession) -> int | None:
    """Fetch + cache the TON→Toman rate (`ton_toman_auto`) when it's plausible. Best-effort.
    Same two-part guard as the USDT rate: an absolute band plus a 3× relative band vs the last
    value (so a Wallex Toman→Rial unit slip = 10× is rejected)."""
    rate = await fetch_ton_toman()
    if not rate or rate < _TON_MIN or rate > _TON_MAX:
        if rate:
            log.warning("fetched TON rate %s outside absolute band — ignored", rate)
        return None
    prev = _pos_int(await settings_service.get(session, "ton_toman_auto", 0))
    if prev and not (prev / 3 <= rate <= prev * 3):
        log.warning("fetched TON rate %s is >3× off the previous %s — ignored", rate, prev)
        return None
    await settings_service.set_value(session, "ton_toman_auto", rate)
    return rate


async def get_ton_toman(session: AsyncSession) -> int:
    """Last cached TON→Toman rate (0 if never fetched / unavailable)."""
    return _pos_int(await settings_service.get(session, "ton_toman_auto", 0)) or 0


def _rate_is_fresh(stamp: str | None, max_age_hours: float) -> bool:
    """True if the cached auto-rate timestamp is within max_age_hours of now. A missing or
    unparseable stamp, or max_age_hours<=0 (disabled), is treated as NOT fresh / always-fresh
    respectively (see caller)."""
    if max_age_hours <= 0:
        return True  # staleness check disabled → always accept the cached rate
    if not stamp:
        return False
    try:
        ts = dt.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - ts
    return age <= dt.timedelta(hours=max_age_hours)


async def get_effective_rate(session: AsyncSession) -> int:
    """The Toman-per-USDT rate to actually use (no network I/O).

    `auto` → the last-fetched live rate IF it's still fresh (younger than `rate_max_age_hours`,
    default 48h); a stale or missing live rate falls back to the manual rate so billing never
    silently uses a days-old quote when the source has been down. `manual` → the owner-set rate.
    """
    cfg = await settings_service.get_many(
        session,
        ["rate_mode", "toman_per_usdt", "toman_per_usdt_auto", "toman_per_usdt_auto_at",
         "rate_max_age_hours"],
    )

    def _int(v) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    manual = _int(cfg.get("toman_per_usdt"))
    if str(cfg.get("rate_mode") or "manual").lower() == "auto":
        auto = _int(cfg.get("toman_per_usdt_auto"))
        max_age = _int(cfg.get("rate_max_age_hours")) or 48
        if auto > 0 and _rate_is_fresh(cfg.get("toman_per_usdt_auto_at"), max_age):
            return auto
        if auto > 0:
            log.warning("auto rate is stale (>%dh) — falling back to manual rate %s", max_age, manual)
        return manual
    return manual
