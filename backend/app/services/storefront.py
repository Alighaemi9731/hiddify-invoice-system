"""Storefront-bot configuration & CRUD (the reseller↔customer subsystem).

Owns the storefront bot record (token encrypted at rest), plans, customers, tenant resolution, and the
owner's monthly-fee computation. Money movement lives in `storefront_wallet`; provisioning in
`storefront_provision`.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.models import Reseller, StorefrontBot, StorefrontCustomer, StorefrontPlan
from app.services import settings_service

# Refresh last_seen_at at most this often (avoid a write on every single interaction; 6h granularity
# is ample for a 90-day retention window).
_SEEN_REFRESH = dt.timedelta(hours=6)

# ── bot record ────────────────────────────────────────────────────────────────

async def get_bot_for_reseller(session: AsyncSession, reseller_id: int) -> StorefrontBot | None:
    return (
        await session.execute(
            select(StorefrontBot).where(StorefrontBot.reseller_id == reseller_id)
        )
    ).scalar_one_or_none()


async def get_bot_by_telegram_id(session: AsyncSession, bot_telegram_id: int) -> StorefrontBot | None:
    """Tenant resolution: which storefront does this physical bot belong to?"""
    return (
        await session.execute(
            select(StorefrontBot).where(StorefrontBot.bot_telegram_id == bot_telegram_id).limit(1)
        )
    ).scalars().first()


async def active_bots(session: AsyncSession) -> list[StorefrontBot]:
    """Enabled storefront bots the manager should be polling."""
    return list(
        (
            await session.execute(select(StorefrontBot).where(StorefrontBot.enabled.is_(True)))
        ).scalars().all()
    )


def bot_token(bot: StorefrontBot) -> str | None:
    return crypto.decrypt(bot.bot_token_enc)


async def upsert_bot(
    session: AsyncSession,
    *,
    reseller_id: int,
    panel_id: int,
    token: str,
    bot_username: str | None,
    bot_telegram_id: int | None,
) -> StorefrontBot:
    """Create or re-point the reseller's storefront bot (token encrypted). One per reseller."""
    bot = await get_bot_for_reseller(session, reseller_id)
    if bot is None:
        bot = StorefrontBot(reseller_id=reseller_id, panel_id=panel_id)
        session.add(bot)
    bot.panel_id = panel_id
    bot.bot_token_enc = crypto.encrypt(token) or ""
    bot.bot_username = bot_username
    bot.bot_telegram_id = bot_telegram_id
    bot.enabled = True
    bot.status = "active"
    bot.last_error = None
    await session.commit()
    return bot


async def mark_errored(session: AsyncSession, bot_id: int, error: str) -> None:
    bot = await session.get(StorefrontBot, bot_id)
    if bot is not None:
        bot.status = "errored"
        bot.last_error = (error or "")[:300]
        await session.commit()


# ── plans ───────────────────────────────────────────────────────────────────

async def list_plans(
    session: AsyncSession, storefront_bot_id: int, *, only_enabled: bool = False
) -> list[StorefrontPlan]:
    q = select(StorefrontPlan).where(StorefrontPlan.storefront_bot_id == storefront_bot_id)
    if only_enabled:
        q = q.where(StorefrontPlan.enabled.is_(True))
    q = q.order_by(StorefrontPlan.sort_order, StorefrontPlan.id)
    return list((await session.execute(q)).scalars().all())


async def add_plan(
    session: AsyncSession, storefront_bot_id: int, *, title: str, gb: int, days: int, price_toman: int
) -> StorefrontPlan:
    existing = await list_plans(session, storefront_bot_id)
    plan = StorefrontPlan(
        storefront_bot_id=storefront_bot_id, title=title[:128], gb=int(gb), days=int(days),
        price_toman=int(price_toman), enabled=True, sort_order=len(existing),
    )
    session.add(plan)
    await session.commit()
    return plan


async def delete_plan(session: AsyncSession, storefront_bot_id: int, plan_id: int) -> bool:
    plan = await session.get(StorefrontPlan, plan_id)
    if plan is None or plan.storefront_bot_id != storefront_bot_id:
        return False
    await session.delete(plan)
    await session.commit()
    return True


async def update_plan(
    session: AsyncSession, storefront_bot_id: int, plan_id: int, *,
    gb: int, days: int, price_toman: int,
) -> bool:
    """Edit a plan's figures in place (ownership-checked). Title stays empty (owner: «عنوان نمی‌خواهیم»)."""
    plan = await session.get(StorefrontPlan, plan_id)
    if plan is None or plan.storefront_bot_id != storefront_bot_id:
        return False
    plan.gb = int(gb)
    plan.days = int(days)
    plan.price_toman = int(price_toman)
    await session.commit()
    return True


async def move_plan(
    session: AsyncSession, storefront_bot_id: int, plan_id: int, direction: str
) -> bool:
    """Reorder a plan up/down by swapping `sort_order` with its adjacent sibling (so a new plan
    needn't be added at the bottom and re-created to reorder). Ownership-checked; a no-op at the
    edge. Normalizes sort_order to the current display order first, so swaps are always well-defined
    even if legacy rows share/duplicate sort_order values."""
    plans = await list_plans(session, storefront_bot_id)
    # Normalize to a dense 0..n-1 sequence in the current (sort_order, id) display order.
    for idx, p in enumerate(plans):
        if p.sort_order != idx:
            p.sort_order = idx
    pos = next((i for i, p in enumerate(plans) if p.id == plan_id), None)
    if pos is None:
        return False
    swap = pos - 1 if direction == "up" else pos + 1
    if swap < 0 or swap >= len(plans):
        await session.commit()  # persist any normalization even on an edge no-op
        return False
    plans[pos].sort_order, plans[swap].sort_order = plans[swap].sort_order, plans[pos].sort_order
    await session.commit()
    return True


# ── customers ─────────────────────────────────────────────────────────────────

async def get_or_create_customer(
    session: AsyncSession, storefront_bot_id: int, tg_user
) -> StorefrontCustomer:  # noqa: ANN001
    cust = (
        await session.execute(
            select(StorefrontCustomer).where(
                StorefrontCustomer.storefront_bot_id == storefront_bot_id,
                StorefrontCustomer.telegram_id == tg_user.id,
            )
        )
    ).scalar_one_or_none()
    name = getattr(tg_user, "first_name", None) or getattr(tg_user, "username", None) or ""
    username = getattr(tg_user, "username", None)
    now = dt.datetime.now(dt.timezone.utc)
    if cust is None:
        cust = StorefrontCustomer(
            storefront_bot_id=storefront_bot_id, telegram_id=tg_user.id,
            name=name[:128] if name else None, username=username, last_seen_at=now,
        )
        session.add(cust)
        await session.commit()
        return cust
    changed = False
    if (cust.name or "") != (name or "")[:128] or cust.username != username:
        cust.name = name[:128] if name else None
        cust.username = username
        changed = True
    seen = cust.last_seen_at
    if seen is None or seen.tzinfo is None or (now - seen) > _SEEN_REFRESH:
        cust.last_seen_at = now  # activity heartbeat for retention (coarse, to bound writes)
        changed = True
    if changed:
        await session.commit()
    return cust


async def list_customers(session: AsyncSession, storefront_bot_id: int) -> list[StorefrontCustomer]:
    return list(
        (
            await session.execute(
                select(StorefrontCustomer)
                .where(StorefrontCustomer.storefront_bot_id == storefront_bot_id)
                .order_by(StorefrontCustomer.created_at.desc())
            )
        ).scalars().all()
    )


async def count_customers(session: AsyncSession, storefront_bot_id: int) -> int:
    return int((await session.execute(
        select(func.count()).select_from(StorefrontCustomer)
        .where(StorefrontCustomer.storefront_bot_id == storefront_bot_id)
    )).scalar_one())


async def list_customers_page(
    session: AsyncSession, storefront_bot_id: int, *, offset: int = 0, limit: int = 8,
    query: str | None = None,
) -> tuple[list[StorefrontCustomer], int]:
    """A page of this storefront's customers (+ the total count), newest first. `query` filters by name
    substring (case-insensitive) OR an exact numeric telegram_id — for the searchable admin list."""
    where: list = [StorefrontCustomer.storefront_bot_id == storefront_bot_id]
    q = (query or "").strip()
    if q:
        conds: list = [StorefrontCustomer.name.ilike(f"%{q}%")]
        if q.lstrip("-").isdigit():
            conds.append(StorefrontCustomer.telegram_id == int(q))
        where.append(or_(*conds))
    total = int((await session.execute(
        select(func.count()).select_from(StorefrontCustomer).where(*where)
    )).scalar_one())
    rows = (await session.execute(
        select(StorefrontCustomer).where(*where)
        .order_by(StorefrontCustomer.created_at.desc()).offset(offset).limit(limit)
    )).scalars().all()
    return list(rows), total


# ── owner monthly fee ──────────────────────────────────────────────────────────

async def monthly_fee_for(session: AsyncSession, reseller: Reseller) -> int:
    """The owner's monthly storefront fee to bill THIS reseller — only when they actually have an
    active storefront bot. Per-reseller override falls back to the global default."""
    if not getattr(reseller, "storefront_enabled", False):
        return 0
    bot = await get_bot_for_reseller(session, reseller.id)
    if bot is None or not bot.enabled:
        return 0  # enabled flag but no active bot → no fee (active-only billing)
    fee = reseller.storefront_monthly_fee_toman
    if fee is None:
        fee = await settings_service.get(session, "storefront_monthly_fee_toman", 0)
    try:
        return max(0, int(fee or 0))
    except (TypeError, ValueError):
        return 0
