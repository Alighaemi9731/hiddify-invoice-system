"""Storefront wallet CREDIT CODES («کد شارژ/هدیه») — validate + redeem + admin CRUD.

A code credits the customer's wallet: a percentage bonus on a top-up (capped by `max_bonus_toman`),
a fixed bonus on a top-up, or — when `is_gift` — a standalone fixed gift credited with no payment.
Usage is counted per-customer and globally; the atomic redeem path locks the code row (Postgres
`FOR UPDATE`) so two customers can't both take the last use. Nothing here touches plan prices — it's
purely the reseller↔customer wallet ledger.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StorefrontCreditCode, StorefrontCreditRedemption

# Persian/Arabic-Indic digits → ASCII, then upper-case → case-insensitive matching.
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def normalize_code(raw: str | None) -> str:
    return (raw or "").translate(_DIGITS).strip().upper()


def _aware(d: dt.datetime | None) -> dt.datetime | None:
    return d if (d is None or d.tzinfo is not None) else d.replace(tzinfo=dt.timezone.utc)


@dataclass
class CreditQuote:
    ok: bool
    reason: str | None = None   # not_found|disabled|not_started|expired|min_not_met|
                                # per_customer_exhausted|global_exhausted|no_bonus
    code_id: int | None = None
    bonus_toman: int = 0
    is_gift: bool = False


# ── admin CRUD ────────────────────────────────────────────────────────────────

async def list_codes(session: AsyncSession, storefront_bot_id: int) -> list[StorefrontCreditCode]:
    return list((await session.execute(
        select(StorefrontCreditCode)
        .where(StorefrontCreditCode.storefront_bot_id == storefront_bot_id)
        .order_by(StorefrontCreditCode.id.desc())
    )).scalars().all())


async def has_enabled_codes(session: AsyncSession, storefront_bot_id: int) -> bool:
    """Any enabled code exists for this shop (drives whether the top-up flow offers the code step)."""
    return (await session.execute(
        select(StorefrontCreditCode.id).where(
            StorefrontCreditCode.storefront_bot_id == storefront_bot_id,
            StorefrontCreditCode.enabled.is_(True),
        ).limit(1)
    )).first() is not None


async def has_gift_codes(session: AsyncSession, storefront_bot_id: int) -> bool:
    """Any enabled GIFT code exists (drives the «کدِ هدیه» entry on the wallet screen)."""
    return (await session.execute(
        select(StorefrontCreditCode.id).where(
            StorefrontCreditCode.storefront_bot_id == storefront_bot_id,
            StorefrontCreditCode.enabled.is_(True),
            StorefrontCreditCode.is_gift.is_(True),
        ).limit(1)
    )).first() is not None


async def get_code(session: AsyncSession, storefront_bot_id: int, code_id: int) -> StorefrontCreditCode | None:
    c = await session.get(StorefrontCreditCode, code_id)
    return c if (c is not None and c.storefront_bot_id == storefront_bot_id) else None


async def code_exists(session: AsyncSession, storefront_bot_id: int, code: str) -> bool:
    return (await session.execute(
        select(StorefrontCreditCode.id).where(
            StorefrontCreditCode.storefront_bot_id == storefront_bot_id,
            StorefrontCreditCode.code_ci == normalize_code(code),
        ).limit(1)
    )).first() is not None


async def create_code(
    session: AsyncSession, storefront_bot_id: int, *, code: str, kind: str,
    percent_off: int | None = None, amount_toman: int | None = None,
    max_bonus_toman: int | None = None, min_topup_toman: int = 0, is_gift: bool = False,
    max_uses: int | None = None, per_customer_limit: int | None = 1,
    starts_at: dt.datetime | None = None, expires_at: dt.datetime | None = None,
) -> StorefrontCreditCode:
    c = StorefrontCreditCode(
        storefront_bot_id=storefront_bot_id, code=code.strip()[:32], code_ci=normalize_code(code)[:32],
        kind=kind, percent_off=percent_off, amount_toman=amount_toman,
        max_bonus_toman=max_bonus_toman, min_topup_toman=int(min_topup_toman or 0), is_gift=is_gift,
        max_uses=max_uses, per_customer_limit=per_customer_limit,
        starts_at=starts_at, expires_at=expires_at, enabled=True,
    )
    session.add(c)
    await session.commit()
    return c


async def set_enabled(session: AsyncSession, code: StorefrontCreditCode, enabled: bool) -> None:
    code.enabled = enabled
    await session.commit()


async def delete_code(session: AsyncSession, storefront_bot_id: int, code_id: int) -> bool:
    c = await get_code(session, storefront_bot_id, code_id)
    if c is None:
        return False
    await session.delete(c)
    await session.commit()
    return True


# ── validation / redemption ───────────────────────────────────────────────────

async def count_for_customer(session: AsyncSession, code_id: int, customer_id: int) -> int:
    return int((await session.execute(
        select(func.count()).select_from(StorefrontCreditRedemption).where(
            StorefrontCreditRedemption.code_id == code_id,
            StorefrontCreditRedemption.customer_id == customer_id,
        )
    )).scalar() or 0)


def _compute(code: StorefrontCreditCode, topup_toman: int) -> tuple[int | None, str | None]:
    """(bonus, reason). A gift ignores the top-up (standalone free credit); a bonus code needs a real
    qualifying top-up (so it can't be redeemed standalone for free)."""
    if code.is_gift:
        return int(code.amount_toman or 0), None
    if topup_toman <= 0 or topup_toman < int(code.min_topup_toman or 0):
        return None, "min_not_met"
    if code.kind == "percent":
        bonus = topup_toman * int(code.percent_off or 0) // 100
        if code.max_bonus_toman is not None:
            bonus = min(bonus, int(code.max_bonus_toman))
        return bonus, None
    return int(code.amount_toman or 0), None  # fixed bonus


async def _evaluate(
    session: AsyncSession, code: StorefrontCreditCode | None, *, topup_toman: int, customer_id: int
) -> CreditQuote:
    if code is None:
        return CreditQuote(False, "not_found")
    if not code.enabled:
        return CreditQuote(False, "disabled", code.id, is_gift=code.is_gift)
    now = dt.datetime.now(dt.timezone.utc)
    if (sa := _aware(code.starts_at)) and now < sa:
        return CreditQuote(False, "not_started", code.id, is_gift=code.is_gift)
    if (ea := _aware(code.expires_at)) and now > ea:
        return CreditQuote(False, "expired", code.id, is_gift=code.is_gift)
    if code.per_customer_limit is not None:
        if await count_for_customer(session, code.id, customer_id) >= int(code.per_customer_limit):
            return CreditQuote(False, "per_customer_exhausted", code.id, is_gift=code.is_gift)
    if code.max_uses is not None and int(code.used_count) >= int(code.max_uses):
        return CreditQuote(False, "global_exhausted", code.id, is_gift=code.is_gift)
    bonus, reason = _compute(code, topup_toman)
    if reason:
        return CreditQuote(False, reason, code.id, is_gift=code.is_gift)
    if not bonus or bonus <= 0:
        return CreditQuote(False, "no_bonus", code.id, is_gift=code.is_gift)
    return CreditQuote(True, None, code.id, int(bonus), code.is_gift)


async def quote(
    session: AsyncSession, storefront_bot_id: int, code_str: str, *,
    topup_toman: int, customer_id: int,
) -> CreditQuote:
    """Advisory validation (no lock, no write) — used to preview the bonus before the customer pays."""
    code = (await session.execute(
        select(StorefrontCreditCode).where(
            StorefrontCreditCode.storefront_bot_id == storefront_bot_id,
            StorefrontCreditCode.code_ci == normalize_code(code_str),
        )
    )).scalar_one_or_none()
    return await _evaluate(session, code, topup_toman=topup_toman, customer_id=customer_id)


def _lock(stmt):  # noqa: ANN001, ANN202
    try:
        return stmt.with_for_update()
    except Exception:  # noqa: BLE001 — SQLite has no row locks (writes serialize anyway)
        return stmt


async def reserve(
    session: AsyncSession, storefront_bot_id: int, code_str: str, *,
    topup_toman: int, customer_id: int,
) -> CreditQuote:
    """Authoritative validate: lock the code row, re-check, and (if ok) increment `used_count` — in
    the caller's transaction (caller commits). Two customers racing for the last use serialize on the
    FOR UPDATE lock; the loser gets `global_exhausted`. Call `record(...)` after to log the redemption
    once the wallet-txn id is known."""
    code = (await session.execute(_lock(select(StorefrontCreditCode).where(
        StorefrontCreditCode.storefront_bot_id == storefront_bot_id,
        StorefrontCreditCode.code_ci == normalize_code(code_str),
    )))).scalar_one_or_none()
    q = await _evaluate(session, code, topup_toman=topup_toman, customer_id=customer_id)
    if q.ok and code is not None:
        code.used_count = int(code.used_count) + 1
    return q


async def reserve_by_id(
    session: AsyncSession, code_id: int, *, storefront_bot_id: int,
    topup_toman: int, customer_id: int,
) -> CreditQuote:
    """Like `reserve`, but by code id — used at top-up confirmation (the code was captured on the
    pending top-up at request time)."""
    code = (await session.execute(_lock(select(StorefrontCreditCode).where(
        StorefrontCreditCode.id == code_id,
        StorefrontCreditCode.storefront_bot_id == storefront_bot_id,
    )))).scalar_one_or_none()
    q = await _evaluate(session, code, topup_toman=topup_toman, customer_id=customer_id)
    if q.ok and code is not None:
        code.used_count = int(code.used_count) + 1
    return q


async def record(
    session: AsyncSession, code_id: int, customer_id: int, wallet_txn_id: int | None, bonus_toman: int
) -> None:
    """Log a redemption (per-customer + audit). Call after a successful `reserve*`, once the bonus
    wallet-txn id is known. Caller commits."""
    session.add(StorefrontCreditRedemption(
        code_id=code_id, customer_id=customer_id, wallet_txn_id=wallet_txn_id,
        bonus_toman=int(bonus_toman)))


# ── analytics ─────────────────────────────────────────────────────────────────

async def redemptions_this_month(session: AsyncSession, storefront_bot_id: int) -> tuple[int, int]:
    """(#redemptions, total bonus toman) for this shop's codes since the 1st of the month."""
    from app.services.periods import current_month

    m = current_month().start
    start = dt.datetime(m.year, m.month, m.day, tzinfo=dt.timezone.utc)
    row = (await session.execute(
        select(func.count(), func.coalesce(func.sum(StorefrontCreditRedemption.bonus_toman), 0))
        .select_from(StorefrontCreditRedemption)
        .join(StorefrontCreditCode, StorefrontCreditCode.id == StorefrontCreditRedemption.code_id)
        .where(
            StorefrontCreditCode.storefront_bot_id == storefront_bot_id,
            StorefrontCreditRedemption.created_at >= start,
        )
    )).first()
    return (int(row[0] or 0), int(row[1] or 0)) if row else (0, 0)


async def usage_summary(session: AsyncSession, code_id: int) -> tuple[int, int, int]:
    """(total redemptions, unique customers, total bonus toman) for one code."""
    row = (await session.execute(
        select(
            func.count(),
            func.count(func.distinct(StorefrontCreditRedemption.customer_id)),
            func.coalesce(func.sum(StorefrontCreditRedemption.bonus_toman), 0),
        ).where(StorefrontCreditRedemption.code_id == code_id)
    )).first()
    return (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)) if row else (0, 0, 0)
