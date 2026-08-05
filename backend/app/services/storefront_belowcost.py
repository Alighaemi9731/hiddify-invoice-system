"""One-shot remediation for storefront plans priced below the reseller's own cost.

Before the `storefront_pricing` guard existed, nothing stopped a reseller pricing a plan under what
that quota costs them. The dominant failure was a UNIT slip — `50` typed for 50,000 Toman — and it
was widespread: at the time this was written 34 of 150 stocked shops were affected, 27 of them on
EVERY enabled plan, together carrying ~25.7M Toman of quota the retail price never covered.

This module finds those plans, disables them, closes shops left with nothing to sell, and tells the
owning reseller what happened and how to fix it.

Deliberately owner-triggered (`POST /api/ops/storefront/below-cost-sweep`, `dry_run=True` by
default) and NOT a scheduled job: the floor moves with `default_price_per_gb`, so a recurring
re-scan would mass-disable a hundred healthy shops unattended the first time the owner raises it.
Only `retry_pending_notices` runs on a schedule, and it re-sends DMs for shops already swept —
it never re-scans and never disables.
"""
from __future__ import annotations

import datetime as dt
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Reseller, StorefrontBot, StorefrontPlan
from app.models.enums import DeliveryStatus
from app.services import broadcast, settings_service, storefront, storefront_admin
from app.services import storefront_pricing as sp

log = logging.getLogger("storefront.belowcost")

# Operational state, deliberately NOT registered in settings_service.DEFS: it is sweep bookkeeping,
# not a knob, and must never appear in the owner's settings UI.
STAMP_KEY = "storefront_belowcost_sweep_v1"

CLOSED_TEXT_FA = (
    "🔴 فروشگاه موقتاً بسته است. در حالِ به‌روزرسانیِ قیمتِ پلن‌ها هستیم و به‌زودی دوباره باز می‌شویم."
)

# Definitive delivery outcomes — retrying them forever is pointless (the reseller blocked the bot,
# or never registered). Only `failed` (transport/token) is transient and worth another pass.
_DEFINITIVE = {DeliveryStatus.sent, DeliveryStatus.blocked, DeliveryStatus.unmatched}


@dataclass(frozen=True)
class PlanFinding:
    plan_id: int
    gb: int
    days: int
    price_toman: int
    floor_toman: int


@dataclass
class ShopFinding:
    shop_id: int
    reseller_id: int
    reseller_name: str
    bot_username: str | None
    cost_per_gb: int
    below: list[PlanFinding] = field(default_factory=list)
    enabled_ok: int = 0
    already_closed: bool = False
    reachable: bool = True

    @property
    def will_close(self) -> bool:
        """A shop with nothing left to sell reads as BROKEN («پلنی برای فروش موجود نیست») unless we
        close it explicitly. Never re-close one the reseller already closed themselves."""
        return self.enabled_ok == 0 and not self.already_closed

    def as_dict(self) -> dict:
        return {
            "shop_id": self.shop_id,
            "reseller_id": self.reseller_id,
            "reseller_name": self.reseller_name,
            "bot_username": self.bot_username,
            "cost_per_gb": self.cost_per_gb,
            "enabled_ok": self.enabled_ok,
            "will_close": self.will_close,
            "already_closed": self.already_closed,
            "reachable": self.reachable,
            "below": [
                {"plan_id": p.plan_id, "gb": p.gb, "days": p.days,
                 "price_toman": p.price_toman, "floor_toman": p.floor_toman}
                for p in self.below
            ],
        }


async def scan(session: AsyncSession) -> list[ShopFinding]:
    """Every shop with at least one ENABLED below-cost plan. Pure read — no writes, no stamping."""
    rows = (
        await session.execute(
            select(StorefrontBot, Reseller).join(Reseller, Reseller.id == StorefrontBot.reseller_id)
        )
    ).all()
    findings: list[ShopFinding] = []
    for shop, reseller in rows:
        cost = await sp.cost_per_gb(session, reseller)
        plans = await storefront.list_plans(session, shop.id, only_enabled=True)
        finding = ShopFinding(
            shop_id=shop.id, reseller_id=reseller.id, reseller_name=reseller.name,
            bot_username=shop.bot_username, cost_per_gb=cost,
            already_closed=bool(shop.shop_closed),
            reachable=reseller.bot_chat_id is not None,
        )
        for plan in plans:
            if sp.is_below_cost(cost=cost, gb=plan.gb, price_toman=plan.price_toman):
                finding.below.append(PlanFinding(
                    plan_id=plan.id, gb=int(plan.gb), days=int(plan.days),
                    price_toman=int(plan.price_toman),
                    floor_toman=sp.price_floor(cost, plan.gb),
                ))
            else:
                finding.enabled_ok += 1
        if finding.below:
            findings.append(finding)
    return findings


async def report(session: AsyncSession) -> dict:
    """What the sweep WOULD do, with totals. Safe to call at any time; touches nothing."""
    findings = await scan(session)
    stamp = await _read_stamp(session)
    return {
        "shops_affected": len(findings),
        "plans_affected": sum(len(f.below) for f in findings),
        "shops_would_close": sum(1 for f in findings if f.will_close),
        "resellers_unreachable": sum(1 for f in findings if not f.reachable),
        "uncovered_toman": sum(
            p.floor_toman - p.price_toman for f in findings for p in f.below),
        "already_swept_shops": len(stamp.get("shops", {})),
        "pending_notices": len(_pending_notice_ids(stamp)),
        "shops": [f.as_dict() for f in findings],
    }


def notice_text_fa(finding: ShopFinding, *, disarmed: int = 0) -> str:
    """The reseller's DM: what was wrong, what we did, and exactly how to fix it. Sent on the MAIN
    bot, which only knows `Reseller.bot_chat_id` — shop co-admins are Telegram identities known
    only to the storefront bot and cannot be reached here."""
    lines = [
        "⚠️ اصلاحِ فوریِ قیمتِ پلن‌های فروشگاهِ شما",
        "",
        f"بررسیِ سیستم نشان می‌دهد قیمتِ {len(finding.below)} پلن در فروشگاهِ تلگرامیِ شما از هزینه‌ای "
        "که بابتِ همان حجم به ما می‌پردازید کمتر بوده است؛ یعنی هر فروش برای شما ضرر داشته است.",
        "به احتمالِ زیاد هنگامِ واردکردنِ قیمت «۰۰۰» جا افتاده است — مثلاً به‌جای 50000 نوشته شده 50.",
        "",
        f"هزینهٔ هر گیگابایت برای شما: {finding.cost_per_gb:,} تومان",
        "",
        "این پلن‌ها موقتاً غیرفعال شدند تا با ضرر فروخته نشوند:",
    ]
    lines.extend(
        f"• {p.gb} گیگابایت / {p.days} روز — قیمتِ شما: {p.price_toman:,} تومان — "
        f"کفِ مجاز: {p.floor_toman:,} تومان"
        for p in finding.below
    )
    if finding.will_close:
        lines += [
            "",
            "چون پلنِ فعالی باقی نماند، فروشگاهِ شما هم موقتاً روی حالتِ «بسته» قرار گرفت و "
            "مشتریان پیامِ «به‌زودی باز می‌شویم» را می‌بینند.",
        ]
    if disarmed:
        lines += [
            "",
            f"همچنین «تمدید خودکارِ» {disarmed} سرویس خاموش شد و مبلغِ رزروشدهٔ مشتریان "
            "بازگردانده شد؛ پس از اصلاحِ قیمت، مشتریان می‌توانند دوباره آن را روشن کنند.",
        ]
    lines += [
        "",
        "چه کار کنم؟",
        "۱) قیمتِ هر پلن را به تومان (نه هزار تومان) اصلاح کنید — در ربات یا در پنلِ فروشگاه.",
        "۲) پلن‌ها را دوباره فعال کنید.",
    ]
    if finding.will_close:
        lines.append("۳) فروشگاه را از حالتِ «بسته» خارج کنید.")
    lines += [
        "",
        "از این پس ثبت یا فعال‌کردنِ پلنی که قیمتش از هزینهٔ شما کمتر باشد ممکن نیست و سیستم "
        "پیش از ذخیره جلوی آن را می‌گیرد.",
    ]
    return "\n".join(lines)


async def run_sweep(
    session: AsyncSession, *, dry_run: bool = True, limit: int | None = None,
    bot: Bot | None = None,
) -> dict:
    """Disable every enabled below-cost plan, close shops left with nothing to sell, and DM each
    owning reseller. `dry_run` defaults to True so an accidental POST reports instead of acting.

    Idempotent: the scan only ever selects CURRENTLY enabled below-cost plans, so re-running touches
    nothing already handled and bumps no config version. The per-shop stamp exists for the DM half,
    which can fail independently of the disables."""
    from app.bot.telegram import build_bot

    findings = await scan(session)
    if limit is not None:
        findings = findings[:max(0, int(limit))]
    counts: dict[str, Any] = {
        "dry_run": bool(dry_run), "shops": len(findings),
        "would_disable" if dry_run else "disabled": sum(len(f.below) for f in findings),
        "would_close" if dry_run else "closed": sum(1 for f in findings if f.will_close),
        "would_notify" if dry_run else "notified": sum(1 for f in findings if f.reachable),
        "failed": 0,
    }
    if dry_run or not findings:
        counts["shops_detail"] = [f.as_dict() for f in findings]
        return counts

    stamp = await _read_stamp(session)
    run_id = stamp.get("run_id") or secrets.token_urlsafe(9)
    stamp.setdefault("run_id", run_id)
    stamp.setdefault("started_at", _now_iso())
    shops_stamp: dict = stamp.setdefault("shops", {})

    counts["disabled"] = 0
    counts["closed"] = 0
    counts["notified"] = 0
    own_bot = False
    if bot is None:
        bot = await build_bot(session)
        own_bot = True
    limiter = broadcast.rate_limiter()
    try:
        for finding in findings:
            entry = shops_stamp.setdefault(str(finding.shop_id), {})
            try:
                disabled = await _disable_plans(session, finding, run_id)
                counts["disabled"] += disabled
                entry["disabled_plan_ids"] = sorted(
                    set(entry.get("disabled_plan_ids", [])) | {p.plan_id for p in finding.below})
                if finding.will_close and await _close_if_empty(session, finding, run_id):
                    counts["closed"] += 1
                    entry["closed"] = True
            except Exception:  # noqa: BLE001 — one bad shop must never abort the sweep
                counts["failed"] += 1
                log.warning("below-cost sweep failed for shop %s", finding.shop_id, exc_info=True)
                continue
            if entry.get("notified_at"):
                continue
            status = await _notify(session, finding, bot=bot, limiter=limiter)
            entry["delivery"] = status.value if status is not None else None
            if status in _DEFINITIVE:
                entry["notified_at"] = _now_iso()
                counts["notified"] += 1
            # A `failed` delivery is left unstamped on purpose: `retry_pending_notices` picks it up
            # next pass rather than the reseller silently never hearing their shop went dark.
        await _write_stamp(session, stamp)
    finally:
        if own_bot and bot is not None:
            await bot.session.close()
    log.info("below-cost sweep: %s", {k: v for k, v in counts.items() if k != "shops_detail"})
    return counts


async def retry_pending_notices(session: AsyncSession, *, bot: Bot | None = None) -> dict:
    """Re-send the sweep DM for shops that were swept but whose notice hit a transient failure.

    NEVER scans and NEVER disables — it only walks shops already recorded in the stamp. That is what
    makes it safe to run on a schedule: raising `default_price_per_gb` cannot turn this into a
    mass-disable of healthy shops."""
    from app.bot.telegram import build_bot

    stamp = await _read_stamp(session)
    pending = _pending_notice_ids(stamp)
    counts = {"pending": len(pending), "sent": 0, "still_failing": 0}
    if not pending:
        return counts

    findings = {f.shop_id: f for f in await scan(session)}
    own_bot = False
    if bot is None:
        bot = await build_bot(session)
        own_bot = True
    limiter = broadcast.rate_limiter()
    try:
        for shop_id in pending:
            finding = findings.get(shop_id) or await _finding_from_history(session, shop_id, stamp)
            if finding is None:
                # Nothing below cost any more and no history to describe — the reseller fixed it
                # before we reached them. Close the entry out rather than retrying forever.
                stamp["shops"][str(shop_id)]["notified_at"] = _now_iso()
                stamp["shops"][str(shop_id)]["delivery"] = "obsolete"
                continue
            status = await _notify(session, finding, bot=bot, limiter=limiter)
            entry = stamp["shops"][str(shop_id)]
            entry["delivery"] = status.value if status is not None else None
            if status in _DEFINITIVE:
                entry["notified_at"] = _now_iso()
                counts["sent"] += 1
            else:
                counts["still_failing"] += 1
        await _write_stamp(session, stamp)
    finally:
        if own_bot and bot is not None:
            await bot.session.close()
    return counts


# ── internals ────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _pending_notice_ids(stamp: dict) -> list[int]:
    return [
        int(shop_id)
        for shop_id, entry in (stamp.get("shops") or {}).items()
        if not entry.get("notified_at")
    ]


async def _read_stamp(session: AsyncSession) -> dict:
    value = await settings_service.get(session, STAMP_KEY, {})
    return dict(value) if isinstance(value, dict) else {}


async def _write_stamp(session: AsyncSession, stamp: dict) -> None:
    await settings_service.set_value(session, STAMP_KEY, stamp)


def _ctx(run_id: int | str, version: int, key: str) -> storefront_admin.CommandContext:
    """`source="system"` bypasses `_authorized_shop` (no human actor) and stamps the audit role
    `system`, so every write this sweep makes is attributable in `storefront_audit_events`."""
    return storefront_admin.CommandContext(
        actor_telegram_id=0, actor_role="system", source="system",
        idempotency_key=key, expected_version=version,
        correlation_id=f"belowcost-sweep:{run_id}",
    )


async def _disable_plans(session: AsyncSession, finding: ShopFinding, run_id: str) -> int:
    disabled = 0
    for plan in finding.below:
        # The version is re-read before EVERY call: each successful CAS bumps `config_version`, so
        # one cached value would 409 on the second plan of the same shop.
        version = await storefront_admin._current_version(session, finding.shop_id)
        await storefront_admin.set_plan_enabled(
            session, finding.shop_id, plan.plan_id,
            _ctx(run_id, version, f"belowcost-sweep:{run_id}:plan:{plan.plan_id}"),
            enabled=False,
        )
        disabled += 1
    return disabled


async def _close_if_empty(session: AsyncSession, finding: ShopFinding, run_id: str) -> bool:
    """Close the shop only if the disables really did leave nothing enabled. Re-read rather than
    trusting the scan: a concurrent edit may have added a healthy plan meanwhile."""
    remaining = await storefront.list_plans(session, finding.shop_id, only_enabled=True)
    if remaining:
        return False
    shop = await session.get(StorefrontBot, finding.shop_id)
    if shop is None or shop.shop_closed:
        return False
    version = await storefront_admin._current_version(session, finding.shop_id)
    await storefront_admin.update_shop_state(
        session, finding.shop_id,
        _ctx(run_id, version, f"belowcost-sweep:{run_id}:shopstate:{finding.shop_id}"),
        closed=True, closed_text=CLOSED_TEXT_FA,
    )
    return True


async def _notify(
    session: AsyncSession, finding: ShopFinding, *, bot: Bot | None, limiter,  # noqa: ANN001
) -> DeliveryStatus | None:
    from app.services import notifier

    reseller = await session.get(Reseller, finding.reseller_id)
    if reseller is None:
        return None
    # `notifier` has no flood control of its own; borrowing the shared limiter keeps one global
    # send policy so a 34-shop sweep can't trip Telegram's rate limit and lose messages.
    await limiter.acquire()
    entry = await notifier.send_to_reseller(session, reseller, notice_text_fa(finding), bot=bot)
    return entry.status


async def _finding_from_history(
    session: AsyncSession, shop_id: int, stamp: dict
) -> ShopFinding | None:
    """Rebuild a finding for a retry when the plans are already disabled (so `scan` no longer sees
    them) — the reseller still needs to be told which plans went dark and why."""
    entry = (stamp.get("shops") or {}).get(str(shop_id)) or {}
    plan_ids = entry.get("disabled_plan_ids") or []
    if not plan_ids:
        return None
    shop = await session.get(StorefrontBot, shop_id)
    if shop is None:
        return None
    reseller = await session.get(Reseller, shop.reseller_id)
    if reseller is None:
        return None
    cost = await sp.cost_per_gb(session, reseller)
    finding = ShopFinding(
        shop_id=shop.id, reseller_id=reseller.id, reseller_name=reseller.name,
        bot_username=shop.bot_username, cost_per_gb=cost,
        already_closed=bool(shop.shop_closed),
        reachable=reseller.bot_chat_id is not None,
    )
    finding.enabled_ok = len(await storefront.list_plans(session, shop.id, only_enabled=True))
    for plan_id in plan_ids:
        plan = await session.get(StorefrontPlan, plan_id)
        if plan is None or plan.storefront_bot_id != shop.id:
            continue
        finding.below.append(PlanFinding(
            plan_id=plan.id, gb=int(plan.gb), days=int(plan.days),
            price_toman=int(plan.price_toman), floor_toman=sp.price_floor(cost, plan.gb),
        ))
    return finding if finding.below else None
