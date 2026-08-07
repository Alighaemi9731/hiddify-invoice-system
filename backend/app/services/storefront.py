"""Storefront-bot configuration & CRUD (the reseller↔customer subsystem).

Owns the storefront bot record (token encrypted at rest), plans, customers, tenant resolution, and the
owner's monthly-fee computation. Money movement lives in `storefront_wallet`; provisioning in
`storefront_provision`.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.models import Reseller, StorefrontBot, StorefrontCustomer, StorefrontPlan
from app.services import settings_service

log = logging.getLogger("storefront")

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


async def trial_user_uuids(session: AsyncSession, panel_id: int) -> set[str]:
    """Panel-scoped set of live FREE-TRIAL config `panel_user_uuid`s. These are excluded from the
    reseller's invoice (a free giveaway) — passed to the billing engine + metering. Keyed on
    `panel_user_uuid`, which joins to `end_user_snapshots.user_uuid` / `usage_meters.user_uuid`."""
    from app.models import StorefrontOrder

    # ANY trial order that ever had a real panel config (panel_user_uuid set) is a giveaway
    # forever — do NOT filter on status. A trial the customer deleted (status "deleted") or one
    # whose provisioning half-failed after the panel user was created (status "failed") still
    # leaves an end_user_snapshot row; restricting to provisioned/disabled let those lingering
    # snapshots leak back into the reseller's invoice via the deleted-user rule.
    rows = (
        await session.execute(
            select(StorefrontOrder.panel_user_uuid).where(
                StorefrontOrder.panel_id == panel_id,
                StorefrontOrder.is_trial.is_(True),
                StorefrontOrder.panel_user_uuid.is_not(None),
            )
        )
    ).scalars().all()
    return {u for u in rows if u}


async def billing_caps(session: AsyncSession, panel_id: int) -> dict[str, float]:
    """Panel-scoped map `panel_user_uuid → plan GB sold`, for capping the reseller's invoice.

    A storefront renewal sets the panel quota to `current_usage + plan_gb` (cumulative), so the
    config's `usage_limit_GB` inflates every cycle and the reseller invoice — which reads that
    quota — double-counts the previous cycle's consumption. Billing the config at the plan it was
    actually sold (`StorefrontOrder.gb`) instead is the true sale. Passed to the invoice engine as
    `cap_gb_by_uuid` (see `invoice_engine.billable_gb_for_user`), keyed on `panel_user_uuid` which
    joins to `end_user_snapshots.user_uuid`.

    Trials are omitted — they're never billed at all (see `trial_user_uuids`). If two orders somehow
    share a uuid, the largest plan wins (can only ever RAISE the cap, never over-trim a real sale).
    """
    from app.models import StorefrontOrder

    rows = (
        await session.execute(
            select(StorefrontOrder.panel_user_uuid, StorefrontOrder.gb).where(
                StorefrontOrder.panel_id == panel_id,
                StorefrontOrder.is_trial.is_(False),
                StorefrontOrder.panel_user_uuid.is_not(None),
            )
        )
    ).all()
    caps: dict[str, float] = {}
    for uuid, gb in rows:
        if not uuid or gb is None:
            continue
        caps[uuid] = max(caps.get(uuid, 0.0), float(gb))
    return caps


async def active_bots(session: AsyncSession) -> list[StorefrontBot]:
    """Enabled storefront bots the manager should be polling. Only `active` rows qualify —
    `errored` (ambiguous failure) and `revoked` (credential permanently dead, token cleared) are
    both EXCLUDED, otherwise reconcile re-validates a dead token every ~30s forever.
    Re-running the setup wizard (`upsert_bot`) resets status to active and re-arms polling."""
    return list(
        (
            await session.execute(
                select(StorefrontBot).where(
                    StorefrontBot.enabled.is_(True), StorefrontBot.status == "active"
                )
            )
        ).scalars().all()
    )


def bot_token(bot: StorefrontBot) -> str | None:
    return crypto.decrypt(bot.bot_token_enc)


# ── co-admins (extra Telegram ids allowed to manage this shop) ─────────────────
MAX_CO_ADMINS = 10


def co_admin_ids(bot: StorefrontBot) -> list[int]:
    """Parse the comma-separated extra-admin Telegram ids (deduped, order-preserving)."""
    out: list[int] = []
    for part in (bot.co_admin_ids or "").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            v = int(part)
            if v not in out:
                out.append(v)
    return out


def is_shop_admin(bot: StorefrontBot, reseller: Reseller | None, user_id: int) -> bool:
    """True if `user_id` may manage this shop: the owning reseller OR an appointed co-admin."""
    if reseller is not None and reseller.bot_chat_id == user_id:
        return True
    return user_id in co_admin_ids(bot)


# Co-admin (manager) MUTATIONS moved to the audited/idempotent shared command layer
# (`storefront_admin.add_manager` / `remove_manager`, plan 003). The read helpers
# `co_admin_ids` / `is_shop_admin` above remain the single source of truth for gating.


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
    """An AMBIGUOUS failure (e.g. a Telegram-id clash): the token may well still be valid, so it
    is kept. Use `mark_revoked` when the credential itself is provably dead."""
    bot = await session.get(StorefrontBot, bot_id)
    if bot is not None:
        bot.status = "errored"
        bot.last_error = (error or "")[:300]
        await session.commit()


async def mark_revoked(session: AsyncSession, bot_id: int, error: str) -> None:
    """The bot's credential is permanently dead (401 Unauthorized / malformed token).

    Keeping a revoked secret buys us nothing and is one more thing to leak, so the encrypted
    token is CLEARED. It is set to "" rather than NULL: the column is NOT NULL, `upsert_bot`
    already writes `crypto.encrypt(...) or ""`, and `storefront_admin` hashes the raw column
    without a None guard. Every token consumer already treats a falsy token as "bot unavailable".

    The shop row itself is kept — plans, customers, orders and wallet balances hang off it, and
    re-running the setup wizard with a fresh token recovers the whole shop in place.

    The reseller is told once, over the MAIN owner bot (their own bot is dead, so it is the only
    channel left). De-dup piggybacks on the status transition: an already-revoked row is skipped.
    """
    bot = await session.get(StorefrontBot, bot_id)
    if bot is None:
        return
    first_time = bot.status != "revoked"
    bot.status = "revoked"
    bot.bot_token_enc = ""
    bot.last_error = (error or "")[:300]
    await session.commit()
    if first_time:
        await _notify_reseller_token_revoked(session, bot)


async def _notify_reseller_token_revoked(session: AsyncSession, bot: StorefrontBot) -> None:
    """Best-effort — a failed notification must never block the status write."""
    try:
        from app.services import notifier

        reseller = await session.get(Reseller, bot.reseller_id)
        if reseller is None:
            return
        shop = f"@{bot.bot_username}" if bot.bot_username else "ربات فروشگاه شما"
        await notifier.send_to_reseller(
            session,
            reseller,
            "⚠️ توکن ربات فروشگاهی شما دیگر معتبر نیست و فروشگاه موقتاً متوقف شد.\n"
            f"ربات: {shop}\n\n"
            "احتمالاً توکن در BotFather باطل یا بازتولید شده است. برای راه‌اندازی دوباره، از منوی "
            "«فروشگاه من» توکن جدید را ارسال کنید.\n"
            "نگران نباشید: مشتریان، سفارش‌ها، پلن‌ها و موجودی کیف‌پول‌ها همگی حفظ شده‌اند و با "
            "توکن جدید دقیقاً از همین‌جا ادامه پیدا می‌کند.",
        )
    except Exception:  # noqa: BLE001
        log.warning("token-revoked notice failed for storefront bot %s", bot.id, exc_info=True)


# ── plans ───────────────────────────────────────────────────────────────────

async def list_plans(
    session: AsyncSession, storefront_bot_id: int, *, only_enabled: bool = False
) -> list[StorefrontPlan]:
    q = select(StorefrontPlan).where(StorefrontPlan.storefront_bot_id == storefront_bot_id)
    if only_enabled:
        q = q.where(StorefrontPlan.enabled.is_(True))
    q = q.order_by(StorefrontPlan.sort_order, StorefrontPlan.id)
    return list((await session.execute(q)).scalars().all())


# Plan MUTATIONS (create / update / enable / delete / reorder) moved to the audited,
# version-checked, idempotent shared command layer (`storefront_admin`, plan 003). The
# read helper `list_plans` above remains shared by the bot, portal and provisioning paths.


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
    """Non-banned customers of a shop (used by broadcast). Banned customers are excluded —
    a broadcast must not reach someone the admin blocked (matches the expiry-reminder sweep)."""
    return list(
        (
            await session.execute(
                select(StorefrontCustomer)
                .where(
                    StorefrontCustomer.storefront_bot_id == storefront_bot_id,
                    StorefrontCustomer.banned.is_(False),
                )
                .order_by(StorefrontCustomer.created_at.desc())
            )
        ).scalars().all()
    )


async def _expired_customers(
    session: AsyncSession, storefront_bot_id: int
) -> list[StorefrontCustomer]:
    """Non-banned customers who have at least one PROVISIONED config that is already expired
    (days-left < 0), computed with the same snapshot-based math as the near-expiry sweep."""
    from app.models import EndUserSnapshot, StorefrontOrder
    from app.services import storefront_expiry
    from app.services.periods import today as tehran_today

    rows = (
        await session.execute(
            select(StorefrontOrder, StorefrontCustomer)
            .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontOrder.customer_id)
            .where(
                StorefrontCustomer.storefront_bot_id == storefront_bot_id,
                StorefrontCustomer.banned.is_(False),
                StorefrontOrder.status == "provisioned",
            )
        )
    ).all()
    if not rows:
        return []
    uuids = [o.panel_user_uuid for o, _c in rows if o.panel_user_uuid]
    snaps: dict[tuple[int, str], EndUserSnapshot] = {}
    if uuids:
        for sn in (
            await session.execute(
                select(EndUserSnapshot).where(EndUserSnapshot.user_uuid.in_(uuids))
            )
        ).scalars().all():
            snaps[(sn.panel_id, sn.user_uuid)] = sn
    today = tehran_today()
    seen: dict[int, StorefrontCustomer] = {}
    for order, cust in rows:
        snap = (snaps.get((order.panel_id, order.panel_user_uuid or ""))
                if order.panel_id is not None else None)
        days_left = storefront_expiry._days_left(order, snap, today)
        if days_left is not None and days_left < 0 and cust.id not in seen:
            seen[cust.id] = cust
    return list(seen.values())


# Broadcast/direct SEGMENTS an admin can target. Every branch excludes banned customers.
SEGMENTS: tuple[str, ...] = ("all", "expired", "inactive30", "trial_no_purchase")
# Hard ceiling on a single broadcast's recipient snapshot (protects the durable enqueue + worker
# from materializing an unbounded shop). Above this the enqueue is refused.
AUDIENCE_CAP = 50_000


def _sql_segment_query(storefront_bot_id: int, segment: str):  # noqa: ANN202
    """Base SELECT (non-banned customers, newest first) for a SQL-expressible segment, or None for
    'expired' (which is computed per-order in Python) / an unknown segment."""
    from app.models import StorefrontOrder

    base = select(StorefrontCustomer).where(
        StorefrontCustomer.storefront_bot_id == storefront_bot_id,
        StorefrontCustomer.banned.is_(False),
    ).order_by(StorefrontCustomer.created_at.desc(), StorefrontCustomer.id.desc())
    if segment == "all":
        return base
    if segment == "inactive30":
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
        activity = func.coalesce(StorefrontCustomer.last_seen_at, StorefrontCustomer.created_at)
        return base.where(activity < cutoff)
    if segment == "trial_no_purchase":
        paid = exists().where(
            StorefrontOrder.customer_id == StorefrontCustomer.id,
            StorefrontOrder.is_trial.is_(False),
            StorefrontOrder.status.in_(("provisioned", "disabled")),
        )
        return base.where(StorefrontCustomer.free_trial_used.is_(True), ~paid)
    return None


async def customers_in_segment(
    session: AsyncSession, storefront_bot_id: int, segment: str
) -> list[StorefrontCustomer]:
    """Non-banned customers of a shop matching a broadcast SEGMENT, so the admin can target an offer:
      all               — everyone (== list_customers)
      inactive30        — no activity (last_seen, else created) in the last 30 days
      trial_no_purchase — used the free trial but never bought a paid plan
      expired           — has a provisioned config that is already expired
    """
    if segment == "expired":
        return await _expired_customers(session, storefront_bot_id)
    q = _sql_segment_query(storefront_bot_id, segment)
    if q is None:
        return []
    return list((await session.execute(q)).scalars().all())


async def audience_preview(
    session: AsyncSession, storefront_bot_id: int, segment: str, *, sample_size: int = 20
) -> dict:
    """Bounded audience size + a small sample for a segment — WITHOUT materializing every recipient.
    Raises ValueError for an unknown segment (the route maps that to 422). No message is sent."""
    if segment not in SEGMENTS:
        raise ValueError(f"unknown segment: {segment}")
    if segment == "expired":
        # 'expired' is order-derived (days-left in Python); bounded by provisioned orders.
        customers = await _expired_customers(session, storefront_bot_id)
        count = len(customers)
        sample = customers[:sample_size]
    else:
        q = _sql_segment_query(storefront_bot_id, segment)
        assert q is not None
        count = int((await session.execute(
            select(func.count()).select_from(q.order_by(None).subquery())
        )).scalar_one() or 0)
        sample = list((await session.execute(q.limit(sample_size))).scalars().all())
    return {
        "segment": segment,
        "count": count,
        "over_cap": count > AUDIENCE_CAP,
        "sample": [
            {"id": c.id, "name": c.name, "username": c.username, "telegram_id": c.telegram_id}
            for c in sample
        ],
    }


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
    active storefront bot. Per-reseller override falls back to the global default.

    The liveness test is the SAME predicate `active_bots` polls on (`enabled AND status ==
    'active'`), and it must stay that way: billing rent for a shop the manager refuses to poll is
    charging for a bot customers cannot reach. Checking `enabled` alone was the bug — nothing in
    the backend ever writes `enabled = False`, so `revoke_bot` (status → 'revoked', token cleared)
    left the shop looking billable forever, and a zero-usage month then MADE a whole invoice for it
    (see the fee gate in `invoicing._generate`)."""
    if not getattr(reseller, "storefront_enabled", False):
        return 0
    bot = await get_bot_for_reseller(session, reseller.id)
    if bot is None or not bot.enabled or bot.status != "active":
        return 0  # no bot, or one that is not polled (revoked/errored) → no fee
    fee = reseller.storefront_monthly_fee_toman
    if fee is None:
        fee = await settings_service.get(session, "storefront_monthly_fee_toman", 0)
    try:
        return max(0, int(fee or 0))
    except (TypeError, ValueError):
        return 0


# ── admin stats (I11) ────────────────────────────────────────────────────────

@dataclass
class BotStats:
    """Aggregates for the storefront admin «📊 آمار» view. Money is Toman."""
    customers: int = 0
    active_30d: int = 0
    plans_enabled: int = 0
    plans_total: int = 0
    provisioned: int = 0
    expiring_soon: int = 0
    sales_month_toman: float = 0.0
    sales_month_count: int = 0
    topups_month_toman: float = 0.0
    pending_topups: int = 0
    wallet_liability_toman: float = 0.0
    credit_redemptions_month: int = 0
    credit_bonus_month_toman: int = 0


async def stats_for_bot(session: AsyncSession, storefront_bot_id: int) -> BotStats:
    """One storefront's dashboard numbers: customer counts, plan counts, provisioned +
    near-expiry services, this-month sales (purchase debits minus refunds) and confirmed
    top-ups from the wallet ledger, pending top-ups, and total wallet liability."""
    from app.models import StorefrontOrder, StorefrontWalletTxn
    from app.services import storefront_expiry, storefront_wallet
    from app.services.periods import current_month

    st = BotStats()
    st.customers = await count_customers(session, storefront_bot_id)

    now = dt.datetime.now(dt.timezone.utc)
    st.active_30d = int((await session.execute(
        select(func.count(StorefrontCustomer.id)).where(
            StorefrontCustomer.storefront_bot_id == storefront_bot_id,
            StorefrontCustomer.last_seen_at.is_not(None),
            StorefrontCustomer.last_seen_at >= now - dt.timedelta(days=30),
        )
    )).scalar_one() or 0)

    plans = await list_plans(session, storefront_bot_id)
    st.plans_total = len(plans)
    st.plans_enabled = sum(1 for p in plans if p.enabled)

    st.provisioned = int((await session.execute(
        select(func.count(StorefrontOrder.id))
        .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontOrder.customer_id)
        .where(
            StorefrontCustomer.storefront_bot_id == storefront_bot_id,
            StorefrontOrder.status == "provisioned",
        )
    )).scalar_one() or 0)

    try:
        threshold = int(await settings_service.get(session, "storefront_expiry_notify_days", 3))
    except (TypeError, ValueError):
        threshold = 3
    st.expiring_soon = await storefront_expiry.count_expiring_soon(
        session, storefront_bot_id, max(threshold, 3))

    month_start = current_month().start
    month_start_dt = dt.datetime(
        month_start.year, month_start.month, month_start.day, tzinfo=dt.timezone.utc)

    def _txn_q(*conds):
        return (
            select(func.coalesce(func.sum(StorefrontWalletTxn.amount_toman), 0),
                   func.count(StorefrontWalletTxn.id))
            .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontWalletTxn.customer_id)
            .where(StorefrontCustomer.storefront_bot_id == storefront_bot_id,
                   StorefrontWalletTxn.created_at >= month_start_dt, *conds)
        )

    purchases = (await session.execute(_txn_q(
        StorefrontWalletTxn.kind == "purchase", StorefrontWalletTxn.status == "done"))).one()
    refunds = (await session.execute(_txn_q(
        StorefrontWalletTxn.kind == "refund", StorefrontWalletTxn.status == "done"))).one()
    # purchases are stored as NEGATIVE debits; refunds as positive credits that undo them.
    st.sales_month_toman = float(-(purchases[0] or 0)) - float(refunds[0] or 0)
    st.sales_month_count = int(purchases[1] or 0)

    topups = (await session.execute(_txn_q(
        StorefrontWalletTxn.kind == "topup", StorefrontWalletTxn.status == "confirmed"))).one()
    st.topups_month_toman = float(topups[0] or 0)
    st.pending_topups = len(await storefront_wallet.pending_topups_for_bot(
        session, storefront_bot_id))

    st.wallet_liability_toman = float((await session.execute(
        select(func.coalesce(func.sum(StorefrontCustomer.wallet_balance_toman), 0)).where(
            StorefrontCustomer.storefront_bot_id == storefront_bot_id)
    )).scalar_one() or 0)

    from app.services import storefront_credit
    st.credit_redemptions_month, st.credit_bonus_month_toman = (
        await storefront_credit.redemptions_this_month(session, storefront_bot_id))
    return st
