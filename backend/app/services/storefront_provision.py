"""Provision a VPN config for a storefront purchase.

Reuses `usercreate.create_for_reseller`, which creates the user on the reseller's panel AS the
reseller (`api_key = reseller.admin_uuid`) — so the config counts toward the reseller's own usage that
the owner bills. Returns the subscription link + uuid, or a typed failure the caller turns into a
wallet refund + admin nudge.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import uuid as uuidlib
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import SessionLocal
from app.models import (
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
    StorefrontPlan,
)
from app.services import storefront_wallet, usercreate

log = logging.getLogger("bot.storefront")

# Serialize a single customer's money/provision actions in-process (buy / trial / renew) so a
# double-tap can't mint two configs or double-charge. The durable guards (row-lock debit, free-trial
# compare-and-set) back this up across processes.
_customer_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _customer_lock(sf_id: int, customer_id: int) -> asyncio.Lock:
    key = (sf_id, customer_id)
    lock = _customer_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _customer_locks[key] = lock
    return lock


@dataclass
class ProvisionResult:
    ok: bool
    sub_link: str | None = None
    uuid: str | None = None
    reason: str | None = None      # "capacity" | "limit" | "error"
    message: str | None = None


@dataclass
class PurchaseResult:
    ok: bool
    order_id: int | None = None
    sub_link: str | None = None
    gb: int = 0
    days: int = 0
    label: str | None = None
    reason: str | None = None      # insufficient | plan_gone | disabled | used | capacity | limit | error
    short_toman: int = 0           # how much the wallet is short (reason == insufficient)
    message: str | None = None


@dataclass
class LiveStatus:
    ok: bool                       # False → the panel read failed; show the stored plan only
    used_gb: float = 0.0
    limit_gb: float = 0.0
    remaining_days: int | None = None


async def provision(
    session: AsyncSession,
    bot: StorefrontBot,
    customer: StorefrontCustomer,
    *,
    gb: int,
    days: int,
    label: str | None = None,
    user_uuid: str | None = None,
) -> ProvisionResult:
    """Create ONE config for this purchase on the reseller's chosen panel.

    `label` is the customer-chosen name: it becomes the config's name on the panel AND the sub-link
    `#slug`, so a customer who buys several services can tell them apart. `user_uuid` pins the config's
    uuid (the order pre-generates it so a mid-create crash is recoverable). Falls back to a
    name+telegram-id base when no label is supplied."""
    reseller = await session.get(Reseller, bot.reseller_id)
    if reseller is None:
        return ProvisionResult(False, reason="error", message="reseller not found")
    base = (label or "").strip() or f"{(customer.name or 'cust')[:16]}-{customer.telegram_id}"
    try:
        result = await usercreate.create_for_reseller(
            session, reseller, count=1, gb=int(gb), days=int(days), base_name=base,
            user_uuid=user_uuid,
        )
    except Exception as exc:  # noqa: BLE001 — surface any panel error as a typed failure (caller refunds)
        return ProvisionResult(False, reason="error", message=str(exc)[:300])
    if getattr(result, "error", None):
        return ProvisionResult(False, reason="error", message=str(result.error)[:300])
    if getattr(result, "capacity_blocked", False):
        return ProvisionResult(False, reason="capacity", message="capacity reached")
    if getattr(result, "limit_hit", False) or not result.created:
        return ProvisionResult(False, reason="limit", message="panel user limit hit")
    u = result.created[0]
    return ProvisionResult(True, sub_link=u.sub_link, uuid=u.uuid)


def _to_float(value) -> float:  # noqa: ANN001
    try:
        f = float(value)
        return f if f == f and f not in (float("inf"), float("-inf")) else 0.0
    except (TypeError, ValueError):
        return 0.0


async def live_status(
    session: AsyncSession, bot: StorefrontBot, order: StorefrontOrder
) -> LiveStatus:
    """Read this order's config LIVE from the panel (used GB, limit GB, remaining days). Best-effort:
    any failure (no uuid, panel error, missing user) returns ok=False so the caller falls back to the
    stored plan figures."""
    if not order.panel_user_uuid:
        return LiveStatus(False)
    reseller = await session.get(Reseller, bot.reseller_id)
    if reseller is None:
        return LiveStatus(False)
    from app.models import Panel
    from app.services.panel_client.admin_api import AdminApiClient

    panel = await session.get(Panel, reseller.panel_id)
    if panel is None:
        return LiveStatus(False)
    try:
        data = await AdminApiClient().get_user(
            panel, order.panel_user_uuid, api_key=reseller.admin_uuid
        )
    except Exception:  # noqa: BLE001 — live read is best-effort; caller shows the stored plan
        log.warning("storefront live_status read failed", exc_info=True)
        return LiveStatus(False)
    if not data:
        return LiveStatus(False)
    remaining = data.get("remaining_day")
    try:
        remaining_days = int(remaining) if remaining is not None else None
    except (TypeError, ValueError):
        remaining_days = None
    return LiveStatus(
        True,
        used_gb=_to_float(data.get("current_usage_GB")),
        limit_gb=_to_float(data.get("usage_limit_GB")) or float(order.gb or 0),
        remaining_days=remaining_days,
    )


# ── atomic, crash-safe purchase + free trial ─────────────────────────────────

async def purchase(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    sf_id: int,
    customer_id: int,
    plan_id: int,
    label: str,
) -> PurchaseResult:
    """Buy a plan: charge the wallet and provision the config, atomically and crash-safely.

    Order + debit are committed together in ONE short transaction BEFORE any network call (so money is
    never debited without an order to attribute it to). The order pre-stores the panel uuid, so if the
    process dies during provisioning the reaper can finish or refund it. DB connections are never held
    across the panel/Telegram I/O. Serialized per customer to defeat double-taps."""
    async with _customer_lock(sf_id, customer_id):
        # 1) short txn — validate, pre-generate uuid, create the pending order + debit, commit together
        async with session_factory() as s:
            sf = await s.get(StorefrontBot, sf_id)
            customer = await s.get(StorefrontCustomer, customer_id)
            plan = await s.get(StorefrontPlan, plan_id)
            if sf is None or customer is None:
                return PurchaseResult(False, reason="error")
            if plan is None or plan.storefront_bot_id != sf.id or not plan.enabled:
                return PurchaseResult(False, reason="plan_gone")
            price = int(plan.price_toman)
            bal = int(storefront_wallet.balance(customer))
            if bal < price:
                return PurchaseResult(False, reason="insufficient", short_toman=price - bal)
            new_uuid = str(uuidlib.uuid4())
            order = StorefrontOrder(
                customer_id=customer.id, plan_id=plan.id, panel_id=sf.panel_id,
                label=(label or "").strip()[:64], gb=plan.gb, days=plan.days,
                price_toman=price, status="pending", panel_user_uuid=new_uuid,
            )
            s.add(order)
            await s.flush()
            ok, _txn = await storefront_wallet.charge_purchase(s, customer.id, price, order_id=order.id)
            if not ok:  # lost a race for the balance
                await s.rollback()
                return PurchaseResult(False, reason="insufficient", short_toman=price - bal)
            await s.commit()
            order_id, gb, days = order.id, order.gb, order.days

        # 2) provision OUTSIDE the first transaction (its own short session for the panel write)
        async with session_factory() as s:
            sf = await s.get(StorefrontBot, sf_id)
            customer = await s.get(StorefrontCustomer, customer_id)
            if sf is None or customer is None:
                res = ProvisionResult(False, reason="error", message="bot/customer vanished")
            else:
                res = await provision(s, sf, customer, gb=gb, days=days, label=label,
                                      user_uuid=new_uuid)

        # 3) short txn — finalize: provisioned (+link) or failed (+refund). The pending-order
        #    reaper may have already finalized this order if provisioning ran long (>15 min):
        #    only act while it is still `pending` (CAS), and guard the refund with
        #    order_has_refund — otherwise a slow provision could double-refund or, worse,
        #    provision AND leave the reaper's refund standing (a paid-for-nothing / free config).
        async with session_factory() as s:
            o = await s.get(StorefrontOrder, order_id, with_for_update=True)
            if o is None:
                return PurchaseResult(False, reason="error")
            if o.status != "pending":
                # The reaper already decided this order — respect its outcome.
                if o.status == "provisioned" and o.sub_link:
                    return PurchaseResult(True, order_id=order_id, sub_link=o.sub_link,
                                          gb=gb, days=days, label=label)
                return PurchaseResult(False, order_id=order_id, reason="reaped",
                                      message="سفارش توسط سیستم نهایی شد", gb=gb, days=days,
                                      label=label)
            if res.ok and res.sub_link:
                o.status = "provisioned"
                o.panel_user_uuid = res.uuid or new_uuid
                o.sub_link = res.sub_link
                await s.commit()
                return PurchaseResult(True, order_id=order_id, sub_link=res.sub_link,
                                      gb=gb, days=days, label=label)
            o.status = "failed"
            if o.price_toman and not await storefront_wallet.order_has_refund(s, order_id):
                await storefront_wallet.refund(s, customer_id, price, order_id=order_id,
                                               note=f"provision {res.reason}")
            await s.commit()
            return PurchaseResult(False, order_id=order_id, reason=res.reason or "error",
                                  message=res.message, gb=gb, days=days, label=label)


async def claim_trial(
    session_factory: async_sessionmaker[AsyncSession] = SessionLocal,
    *,
    sf_id: int,
    customer_id: int,
) -> PurchaseResult:
    """Claim the one-time free trial, concurrency-safe: the used-flag is compare-and-set under a row
    lock BEFORE provisioning (reverted only if provisioning fails), so a double-tap mints exactly one."""
    async with _customer_lock(sf_id, customer_id):
        async with session_factory() as s:
            sf = await s.get(StorefrontBot, sf_id)
            if sf is None or not sf.free_trial_enabled:
                return PurchaseResult(False, reason="disabled")
            stmt = select(StorefrontCustomer).where(StorefrontCustomer.id == customer_id)
            try:
                stmt = stmt.with_for_update()
            except Exception:  # noqa: BLE001 — sqlite has no row locks; the in-memory lock covers it
                pass
            customer = (await s.execute(stmt)).scalar_one_or_none()
            if customer is None:
                return PurchaseResult(False, reason="error")
            if customer.free_trial_used:
                return PurchaseResult(False, reason="used")
            gb, days = int(sf.free_trial_gb or 1), int(sf.free_trial_days or 1)
            new_uuid = str(uuidlib.uuid4())
            order = StorefrontOrder(
                customer_id=customer.id, plan_id=None, panel_id=sf.panel_id, label="تست رایگان",
                gb=gb, days=days, price_toman=0, status="pending", panel_user_uuid=new_uuid,
                is_trial=True,
            )
            s.add(order)
            customer.free_trial_used = True  # CAS: claimed before provisioning, under the lock
            await s.commit()
            order_id = order.id

        async with session_factory() as s:
            sf = await s.get(StorefrontBot, sf_id)
            customer = await s.get(StorefrontCustomer, customer_id)
            if sf is None or customer is None:
                res = ProvisionResult(False, reason="error", message="bot/customer vanished")
            else:
                res = await provision(s, sf, customer, gb=gb, days=days, label="تست رایگان",
                                      user_uuid=new_uuid)

        async with session_factory() as s:
            o = await s.get(StorefrontOrder, order_id)
            if o is None:
                return PurchaseResult(False, reason="error")
            if res.ok and res.sub_link:
                o.status = "provisioned"
                o.panel_user_uuid = res.uuid or new_uuid
                o.sub_link = res.sub_link
                await s.commit()
                return PurchaseResult(True, order_id=order_id, sub_link=res.sub_link,
                                      gb=gb, days=days, label="تست رایگان")
            o.status = "failed"
            cust = await s.get(StorefrontCustomer, customer_id)
            if cust is not None:
                cust.free_trial_used = False  # let them retry a failed trial
            await s.commit()
            return PurchaseResult(False, order_id=order_id, reason=res.reason or "error",
                                  message=res.message, gb=gb, days=days, label="تست رایگان")


async def reap_pending_orders(
    session: AsyncSession, *, older_than: dt.datetime
) -> dict[str, int]:
    """Reconcile orders stuck in `pending` past `older_than` (process died mid-purchase). For each, look
    its pre-stored uuid up on the panel: present → mark provisioned + rebuild the sub-link (and best-effort
    notify the customer); absent → refund (once) + mark failed. Idempotent. Returns counts."""
    from app.services.panel_client.admin_api import AdminApiClient

    counts = {"checked": 0, "completed": 0, "refunded": 0}
    stale = (
        await session.execute(
            select(StorefrontOrder).where(
                StorefrontOrder.status == "pending",
                StorefrontOrder.created_at < older_than,
            ).limit(200)
        )
    ).scalars().all()
    client = AdminApiClient()
    for order in stale:
        counts["checked"] += 1
        customer = await session.get(StorefrontCustomer, order.customer_id)
        sf = await session.get(StorefrontBot, customer.storefront_bot_id) if customer else None
        reseller = await session.get(Reseller, sf.reseller_id) if sf else None
        panel = await session.get(Panel, reseller.panel_id) if reseller else None
        if not (customer and sf and reseller and panel and order.panel_user_uuid):
            continue
        try:
            data = await client.get_user(panel, order.panel_user_uuid, api_key=reseller.admin_uuid)
        except Exception:  # noqa: BLE001 — leave it pending; try again next tick
            log.warning("reaper get_user failed for order %s", order.id, exc_info=True)
            continue
        if data:  # the config WAS created before the crash → complete it
            order.status = "provisioned"
            order.sub_link = panel.user_sub_link(order.panel_user_uuid, name=order.label or None)
            await session.commit()
            counts["completed"] += 1
            await _notify_completed(sf, customer, order)
        else:  # nothing on the panel → refund (once) and fail
            if order.price_toman and not await storefront_wallet.order_has_refund(session, order.id):
                await storefront_wallet.refund(session, order.customer_id, int(order.price_toman),
                                               order_id=order.id, note="reaper: provision lost")
            # A reaped-failed FREE TRIAL must free the one-time flag so the customer can retry —
            # a trial has no refund path (price 0), so without this they'd be stuck unable to
            # re-claim (same recovery claim_trial does on its own provisioning failure).
            if order.is_trial and customer.free_trial_used:
                customer.free_trial_used = False
            order.status = "failed"
            await session.commit()
            counts["refunded"] += 1
    return counts


async def _notify_completed(
    sf: StorefrontBot, customer: StorefrontCustomer, order: StorefrontOrder
) -> None:
    """Best-effort: deliver the recovered config's link to the customer from the reaper (backend process,
    so it builds a transient Bot from the stored token). A failure here is harmless — the order is already
    provisioned and visible in «my services»."""
    from aiogram import Bot

    from app.bot.rtl import rtl
    from app.services import storefront

    token = storefront.bot_token(sf)
    if not token or not order.sub_link:
        return
    bot = Bot(token=token)
    try:
        await bot.send_message(
            customer.telegram_id,
            rtl(f"✅ سرویسِ «{order.label or 'شما'}» آماده شد.\n\n🔗 لینکِ اشتراک:\n"
                f"<code>{order.sub_link}</code>"),
            parse_mode="HTML", disable_web_page_preview=True)
    except Exception:  # noqa: BLE001
        log.info("reaper notify failed for order %s", order.id, exc_info=True)
    finally:
        try:
            await bot.session.close()
        except Exception:  # noqa: BLE001
            pass

