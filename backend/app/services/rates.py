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


async def _wallex_usdt(client: httpx.AsyncClient) -> int | None:
    # Wallex: result.symbols.USDTTMN.stats.bidPrice (Toman).
    r = await client.get(_WALLEX)
    r.raise_for_status()
    symbols = ((r.json().get("result") or {}).get("symbols") or {})
    stats = (symbols.get("USDTTMN") or {}).get("stats") or {}
    return _pos_int(stats.get("bidPrice"))


async def _tetherland_usdt(client: httpx.AsyncClient) -> int | None:
    # Tetherland: data.currencies.USDT.price is already in Toman.
    r = await client.get(_TETHERLAND)
    r.raise_for_status()
    usdt = (((r.json().get("data") or {}).get("currencies") or {}).get("USDT") or {})
    return _pos_int(usdt.get("price"))


async def fetch_usdt_toman(source: str = "wallex") -> int | None:
    """Fetch the current USDT price in **Toman** from the configured `source` (wallex|tetherland),
    falling back to the other source. Returns None on any failure — the caller keeps the previous
    value. Both sources quote USDT in Toman directly."""
    order = ["tetherland", "wallex"] if str(source).lower() == "tetherland" else ["wallex", "tetherland"]
    fetchers = {"wallex": _wallex_usdt, "tetherland": _tetherland_usdt}
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "invoice-system/1"}) as client:
        for name in order:
            try:
                rate = await fetchers[name](client)
                if rate:
                    return rate
            except Exception:  # noqa: BLE001 — try the next source
                log.info("%s USDT rate fetch failed, trying next source", name, exc_info=True)
    log.warning("failed to fetch USDT→Toman rate from all sources")
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
    source = str(await settings_service.get(session, "rate_source", "wallex") or "wallex")
    rate = await fetch_usdt_toman(source)
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
    """Fetch the current Gram (formerly Toncoin) price in **Toman** from Wallex. The token was renamed
    TON→GRAM on 2026-06-15 and Wallex relisted the market as `GRAMTMN` (the old `TONTMN` is gone), so we
    try GRAMTMN first and fall back to TONTMN for safety. 1 GRAM = 1 former TON, and the network is still
    TON — only the ticker changed. Display-only; a failure just hides the amount. Returns None on failure."""
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "invoice-system/1"}) as client:
            r = await client.get(_WALLEX)
            r.raise_for_status()
            symbols = ((r.json().get("result") or {}).get("symbols") or {})
            for sym in ("GRAMTMN", "TONTMN"):
                stats = (symbols.get(sym) or {}).get("stats") or {}
                rate = _pos_int(stats.get("bidPrice"))
                if rate:
                    return rate
    except Exception:  # noqa: BLE001
        log.warning("failed to fetch Gram→Toman rate from Wallex", exc_info=True)
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
    await settings_service.set_value(
        session, "ton_toman_auto_at",
        dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )
    return rate


async def get_ton_toman(session: AsyncSession) -> int:
    """The TON→Toman rate to actually use (no network I/O), mirroring the USDT manual/auto split:
    `manual` → the owner-set `ton_toman_manual`; `auto` → the last cached live rate, falling back
    to the manual rate when the live one is missing. 0 if nothing usable."""
    cfg = await settings_service.get_many(
        session, ["ton_rate_mode", "ton_toman_manual", "ton_toman_auto",
                  "ton_toman_auto_at", "rate_max_age_hours"]
    )
    manual = _pos_int(cfg.get("ton_toman_manual")) or 0
    if str(cfg.get("ton_rate_mode") or "auto").lower() == "manual":
        return manual
    auto = _pos_int(cfg.get("ton_toman_auto")) or 0
    # Mirror the USDT staleness guard: a cached auto rate older than rate_max_age_hours is not
    # trusted (Wallex delisted the symbol once, freezing the rate for weeks) — fall back to the
    # manual rate. This is not display-only: it prices the amount the customer is told to send
    # and drives the on-chain «مطابق/مغایر» verdict.
    max_age = _rate_max_age(cfg.get("rate_max_age_hours"))
    if auto > 0 and _rate_is_fresh(cfg.get("ton_toman_auto_at"), max_age):
        return auto
    return manual or auto


# AVAX has no Toman market on Wallex/Tetherland (the only sources reachable from the prod host),
# so its rate is DERIVED: AVAX→USD from CoinGecko (free, no key, resolves internationally) times
# the live USDT→Toman rate (USDT ≈ USD). Display-only; a failure just hides the amount.
_COINGECKO_AVAX = "https://api.coingecko.com/api/v3/simple/price?ids=avalanche-2&vs_currencies=usd"


async def fetch_avax_usd() -> float | None:
    """Fetch the current AVAX price in **USD** from CoinGecko. Returns None on any failure (the
    caller keeps the previous value). Shape: {"avalanche-2": {"usd": <float>}}."""
    try:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "invoice-system/1"}) as client:
            r = await client.get(_COINGECKO_AVAX)
            r.raise_for_status()
            usd = ((r.json().get("avalanche-2") or {}).get("usd"))
            if usd is not None:
                v = float(usd)
                return v if v > 0 else None
    except Exception:  # noqa: BLE001
        log.warning("failed to fetch AVAX→USD rate from CoinGecko", exc_info=True)
    return None


# AVAX→Toman ≈ a few-USD coin × ~100k–200k Toman/USD → low/mid hundreds of k to a few M. The floor
# is kept well below any realistic correct value (≈$0.17-worth) so a market dip never rejects a good
# rate; it only catches a source glitch (cents/garbage). The ceiling guards an obvious unit slip.
_AVAX_MIN, _AVAX_MAX = 30_000, 100_000_000


async def refresh_avax_rate(session: AsyncSession) -> int | None:
    """Fetch AVAX→USD and multiply by the live USDT→Toman rate to cache AVAX→Toman
    (`avax_toman_auto`) when plausible. Best-effort. Same two-part guard as the USDT/TON rates:
    an absolute band plus a 3× relative band vs the last value."""
    usd = await fetch_avax_usd()
    usdt_toman = await get_effective_rate(session)
    if not usd or not usdt_toman:
        return None
    rate = int(round(usd * usdt_toman))
    if rate < _AVAX_MIN or rate > _AVAX_MAX:
        log.warning("derived AVAX rate %s outside absolute band — ignored", rate)
        return None
    prev = _pos_int(await settings_service.get(session, "avax_toman_auto", 0))
    if prev and not (prev / 3 <= rate <= prev * 3):
        log.warning("derived AVAX rate %s is >3× off the previous %s — ignored", rate, prev)
        return None
    await settings_service.set_value(session, "avax_toman_auto", rate)
    await settings_service.set_value(
        session, "avax_toman_auto_at",
        dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )
    return rate


async def get_avax_toman(session: AsyncSession) -> int:
    """The AVAX→Toman rate to actually use (no network I/O), mirroring the TON manual/auto split:
    `manual` → the owner-set `avax_toman_manual`; `auto` → the last cached live rate, falling back
    to the manual rate when the live one is missing. 0 if nothing usable."""
    cfg = await settings_service.get_many(
        session, ["avax_rate_mode", "avax_toman_manual", "avax_toman_auto",
                  "avax_toman_auto_at", "rate_max_age_hours"]
    )
    manual = _pos_int(cfg.get("avax_toman_manual")) or 0
    if str(cfg.get("avax_rate_mode") or "auto").lower() == "manual":
        return manual
    auto = _pos_int(cfg.get("avax_toman_auto")) or 0
    # Same staleness guard as TON/USDT (see get_ton_toman).
    max_age = _rate_max_age(cfg.get("rate_max_age_hours"))
    if auto > 0 and _rate_is_fresh(cfg.get("avax_toman_auto_at"), max_age):
        return auto
    return manual or auto


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


def _rate_max_age(raw) -> float:
    """The configured max cached-rate age in hours: a missing/unparseable value defaults to 48,
    but an explicit 0 stays 0 (disables the staleness check). `x or 48` used to turn 0 into 48,
    making the documented "0 = disabled" branch unreachable."""
    if raw is None or raw == "":
        return 48.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 48.0


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
        max_age = _rate_max_age(cfg.get("rate_max_age_hours"))
        if auto > 0 and _rate_is_fresh(cfg.get("toman_per_usdt_auto_at"), max_age):
            return auto
        if auto > 0:
            log.warning("auto rate is stale (>%dh) — falling back to manual rate %s", max_age, manual)
        return manual
    return manual
