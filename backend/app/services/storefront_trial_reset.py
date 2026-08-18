"""Fleet-wide AUTOMATIC monthly free-trial re-arm.

`v1.114.0` gave every shop admin a once-a-month button to re-arm their customers' free trials. The
button worked; almost nobody pressed it. A win-back feature nobody triggers wins nobody back, so
the platform now runs it for the whole fleet on a schedule and the per-shop trigger is gone.

What this module is NOT: it does not re-implement the reset. Every shop still goes through
`storefront_admin.reset_free_trials`, which owns the once-a-month stamp, the config-version CAS,
the idempotency claim, the bulk re-arm and the durable customer announcement — all in ONE
transaction. This is only the fleet loop around it, in the same shape as
`storefront_belowcost`: `source="system"` (no human actor), one session per shop, one deterministic
idempotency key per shop per period.

Three eligibility rules are load-bearing:

* `active_bots()` — `enabled AND status == 'active'`. `StorefrontBot.enabled` is write-once True,
  so `status` is the only real liveness axis; announcing to a revoked shop's customers through a
  dead token would just fill the delivery queue with permanent failures.
* the shop's own trial must be ON and the shop must not be closed — a closed shop refuses every
  new config including trials, so "your free trial is back" would be a lie in both cases.
* a shop created inside the CURRENT period is skipped. Its customers have not had time to burn a
  trial yet, so there is nothing to re-arm, and «دوباره فعال شد» reads as nonsense to someone
  who never had one. It gets its first re-arm next month.

THE COST IS THE OWNER'S. A trial's quota is excluded from the reseller's invoice at any size
(`storefront.trial_user_uuids` → `invoice_engine`), so every re-armed customer who claims is
platform-funded. Making that monthly turns a one-off giveaway into a recurring one; the bounds are
`storefront_trial_max_gb` and the `storefront_trial_reset_enabled` master switch, nothing else.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.models import StorefrontBot
from app.services import owner_notify, periods, storefront, storefront_admin

log = logging.getLogger("storefront.trialreset")


def _ctx(period: str, version: int) -> storefront_admin.CommandContext:
    """`source="system"` bypasses `_authorized_shop` (there is no human actor) and stamps the audit
    role `system`, so every re-arm stays attributable in `storefront_audit_events`.

    The idempotency key is deliberately CONSTANT for a (shop, period): the sweep runs again on the
    two days after the reset day to heal a shop it could not finish, and a repeat must replay the
    stored response rather than re-announce. `_claim_db_command` keys on
    `(storefront_bot_id, actor_telegram_id, idempotency_key)`, so one key per period is per-shop
    unique already.
    """
    return storefront_admin.CommandContext(
        actor_telegram_id=0, actor_role="system", source="system",
        idempotency_key=f"trial-auto-reset:{period}", expected_version=version,
        correlation_id=f"trial-auto-reset:{period}",
    )


def _eligible(shop: StorefrontBot, period: periods.Period) -> bool:
    if not shop.free_trial_enabled or shop.shop_closed:
        return False
    if shop.trial_reset_period == period.label:
        return False
    created = shop.created_at
    if created is not None and period.contains(periods.to_local_date(created)):
        return False  # brand-new shop: nothing to re-arm, and the wording would not make sense
    return True


async def _candidates(session: AsyncSession, period: periods.Period) -> list[tuple[int, str]]:
    """`(shop_id, label)` for every shop the sweep would act on, newest state re-read each run."""
    shops = await storefront.active_bots(session)
    return [
        (shop.id, shop.bot_username or f"#{shop.id}")
        for shop in shops
        if _eligible(shop, period)
    ]


async def report() -> dict[str, Any]:
    """Read-only view of what the next sweep would do (owner ops endpoint).

    `active_shops` vs `pending_shops` is the useful pair: the first is the fleet the sweep looks
    at, the second is what it would actually re-arm this month — and since each of those is quota
    the platform gives away, the gap is worth seeing before the day rather than after."""
    period = periods.current_month()
    async with SessionLocal() as session:
        enabled = await storefront.trial_reset_enabled(session)
        shops = await storefront.active_bots(session)
        candidates = [s for s in shops if enabled and _eligible(s, period)]
    return {
        "period": period.label,
        "enabled": enabled,
        "active_shops": len(shops),
        "pending_shops": len(candidates),
        "shops": [s.bot_username or f"#{s.id}" for s in candidates],
    }


async def sweep(*, dry_run: bool = False) -> dict[str, Any]:
    """Re-arm every eligible shop's customers for the current Gregorian month.

    One session PER SHOP: `storefront_admin._execute` commits internally, so a shared session would
    make one shop's failure roll back another's committed reset. Every shop is wrapped
    individually — a single bad shop must never stop the fleet — and `already_reset_this_month` is
    counted as a normal outcome, not a failure (it just means the stamp was already there).
    """
    period = periods.current_month()
    counts: dict[str, Any] = {
        "period": period.label, "shops": 0, "customers": 0, "notified": 0,
        "already": 0, "failed": 0, "dry_run": dry_run,
    }
    async with SessionLocal() as session:
        if not await storefront.trial_reset_enabled(session):
            counts["skipped"] = "disabled"
            return counts
        candidates = await _candidates(session, period)
    counts["pending_shops"] = len(candidates)
    if dry_run:
        counts["shops"] = len(candidates)
        return counts

    for shop_id, name in candidates:
        async with SessionLocal() as session:
            try:
                version = await storefront_admin._current_version(session, shop_id)
                result = await storefront_admin.reset_free_trials(
                    session, shop_id, _ctx(period.label, version))
            except storefront_admin.AdminCommandError as exc:
                if exc.code == "already_reset_this_month":
                    counts["already"] += 1
                else:
                    counts["failed"] += 1
                    log.warning("trial auto-reset failed for %s: %s (%s)", name, exc.code, exc)
                continue
            except Exception:  # noqa: BLE001 — one shop must never abort the fleet
                counts["failed"] += 1
                log.exception("trial auto-reset crashed for %s", name)
                continue
            body = result.body if isinstance(result.body, dict) else {}
            counts["shops"] += 1
            counts["customers"] += int(body.get("reset_count") or 0)
            counts["notified"] += int(body.get("notified") or 0)
    if counts["shops"]:
        await _notify_owner(counts)
    return counts


async def _notify_owner(counts: dict[str, Any]) -> None:
    """One calm summary per sweep. Best-effort: a Telegram hiccup must not fail the job."""
    text = (
        "🎁 ریستِ خودکارِ ماهانهٔ تستِ رایگان انجام شد.\n"
        f"دوره: {counts['period']}\n"
        f"فروشگاه: {counts['shops']}\n"
        f"مشتریِ دوباره واجدِ شرایط: {counts['customers']}\n"
        f"پیامِ در صفِ ارسال: {counts['notified']}"
    )
    if counts.get("failed"):
        text += f"\nناموفق: {counts['failed']} فروشگاه (در اجرای روزهای بعد دوباره تلاش می‌شود)"
    try:
        async with SessionLocal() as session:
            await owner_notify.notify_owner(session, text)
    except Exception:  # noqa: BLE001
        log.warning("trial auto-reset owner summary failed", exc_info=True)


def next_reset_date(day: int, today: dt.date | None = None) -> dt.date:
    """The next calendar date the sweep will re-arm on, for customer-facing copy.

    `day` is the owner's configured day of month (clamped 1..28 by settings, so it exists in every
    month). Today counts only if the sweep has not run yet this month — the caller knows that from
    the shop's own `trial_reset_period`, so this stays a pure date helper.
    """
    today = today or periods.today()
    day = max(1, min(28, int(day or 1)))
    if today.day <= day:
        return today.replace(day=day)
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    return dt.date(year, month, day)
