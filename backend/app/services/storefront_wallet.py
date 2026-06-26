"""Storefront wallet ledger (reseller↔customer money — separate from owner↔reseller invoices).

A customer tops up (status `pending` until the reseller-admin confirms → balance credited), then buys
a plan (atomic debit). Manual admin adjustments and refunds are recorded too. The denormalized
`StorefrontCustomer.wallet_balance_toman` is the source of truth for spendable balance; every change
also writes a `StorefrontWalletTxn` row for audit.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StorefrontCustomer, StorefrontWalletTxn


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def balance(customer: StorefrontCustomer) -> Decimal:
    return Decimal(str(customer.wallet_balance_toman or 0))


async def create_topup(
    session: AsyncSession,
    customer: StorefrontCustomer,
    amount_toman: int,
    *,
    method: str,
    proof_path: str | None = None,
    txid: str | None = None,
    chain: str | None = None,
) -> StorefrontWalletTxn:
    """Record a PENDING wallet top-up. Balance is NOT credited until the reseller-admin confirms."""
    txn = StorefrontWalletTxn(
        customer_id=customer.id, kind="topup", amount_toman=Decimal(str(int(amount_toman))),
        status="pending", method=method, proof_path=proof_path, txid=txid, chain=chain,
    )
    session.add(txn)
    await session.commit()
    return txn


async def confirm_topup(
    session: AsyncSession, txn_id: int, *, amount_toman: int | None = None
) -> tuple[bool, StorefrontWalletTxn | None]:
    """Confirm a pending top-up and credit the wallet. `amount_toman` lets the admin set the credited
    Toman (used for crypto deposits — manual, no rates). Idempotent: a second confirm is a no-op.
    Returns (changed, txn)."""
    txn = await session.get(StorefrontWalletTxn, txn_id)
    if txn is None or txn.kind != "topup":
        return False, txn
    if txn.status != "pending":
        return False, txn  # already decided — no double credit
    customer = await session.get(StorefrontCustomer, txn.customer_id)
    if customer is None:
        return False, txn
    # The admin may override the Toman to credit (esp. for crypto deposits, where the amount is set
    # manually — the bot never depends on online rates). Otherwise credit the requested amount.
    credited = Decimal(str(int(amount_toman))) if amount_toman is not None else Decimal(
        str(txn.amount_toman or 0)
    )
    if credited < 0:
        credited = Decimal(0)
    customer.wallet_balance_toman = float(balance(customer) + credited)
    txn.amount_toman = float(credited)
    txn.status = "confirmed"
    txn.decided_at = _now()
    await session.commit()
    return True, txn


async def reject_topup(session: AsyncSession, txn_id: int) -> tuple[bool, StorefrontWalletTxn | None]:
    """Reject a pending top-up (no credit). Idempotent. Returns (changed, txn)."""
    txn = await session.get(StorefrontWalletTxn, txn_id)
    if txn is None or txn.kind != "topup":
        return False, txn
    if txn.status != "pending":
        return False, txn
    txn.status = "rejected"
    txn.decided_at = _now()
    await session.commit()
    return True, txn


async def manual_adjust(
    session: AsyncSession, customer: StorefrontCustomer, amount_toman_signed: int, *, note: str = ""
) -> StorefrontWalletTxn:
    """Admin manually credits (+) or debits (−) a customer's wallet. Never drives the balance below 0."""
    delta = Decimal(str(int(amount_toman_signed)))
    new_balance = balance(customer) + delta
    if new_balance < 0:
        new_balance = Decimal(0)
        delta = new_balance - balance(customer)
    customer.wallet_balance_toman = float(new_balance)
    txn = StorefrontWalletTxn(
        customer_id=customer.id,
        kind="manual_credit" if delta >= 0 else "manual_debit",
        amount_toman=delta, status="done", method="manual", note=(note or "")[:255],
        decided_at=_now(),
    )
    session.add(txn)
    await session.commit()
    return txn


async def charge_purchase(
    session: AsyncSession, customer_id: int, price_toman: int
) -> tuple[bool, StorefrontWalletTxn | None]:
    """Atomically debit the wallet for a purchase. Re-reads the customer under a row lock (Postgres)
    so two concurrent buys can't overspend. Returns (ok, debit_txn). ok=False if the balance is short."""
    price = Decimal(str(int(price_toman)))
    stmt = select(StorefrontCustomer).where(StorefrontCustomer.id == customer_id)
    try:
        stmt = stmt.with_for_update()
    except Exception:  # noqa: BLE001 — sqlite has no row locks; the test path is single-threaded
        pass
    customer = (await session.execute(stmt)).scalar_one_or_none()
    if customer is None or balance(customer) < price:
        return False, None
    customer.wallet_balance_toman = float(balance(customer) - price)
    txn = StorefrontWalletTxn(
        customer_id=customer.id, kind="purchase", amount_toman=-price, status="done",
        decided_at=_now(),
    )
    session.add(txn)
    await session.commit()
    return True, txn


async def refund(
    session: AsyncSession, customer_id: int, amount_toman: int, *, note: str = ""
) -> StorefrontWalletTxn | None:
    """Credit a refund back to the wallet (e.g. provisioning failed after the debit)."""
    customer = await session.get(StorefrontCustomer, customer_id)
    if customer is None:
        return None
    amt = Decimal(str(int(amount_toman)))
    customer.wallet_balance_toman = float(balance(customer) + amt)
    txn = StorefrontWalletTxn(
        customer_id=customer.id, kind="refund", amount_toman=amt, status="done",
        note=(note or "")[:255], decided_at=_now(),
    )
    session.add(txn)
    await session.commit()
    return txn


async def pending_topups_for_bot(
    session: AsyncSession, storefront_bot_id: int
) -> list[StorefrontWalletTxn]:
    """All pending top-ups across this storefront's customers (for the admin review queue)."""
    rows = (
        await session.execute(
            select(StorefrontWalletTxn)
            .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontWalletTxn.customer_id)
            .where(
                StorefrontCustomer.storefront_bot_id == storefront_bot_id,
                StorefrontWalletTxn.kind == "topup",
                StorefrontWalletTxn.status == "pending",
            )
            .order_by(StorefrontWalletTxn.created_at)
        )
    ).scalars().all()
    return list(rows)


async def pending_topups_for_customer(
    session: AsyncSession, customer_id: int
) -> list[StorefrontWalletTxn]:
    """This customer's own pending top-ups (shown on their wallet screen)."""
    rows = (
        await session.execute(
            select(StorefrontWalletTxn).where(
                StorefrontWalletTxn.customer_id == customer_id,
                StorefrontWalletTxn.kind == "topup",
                StorefrontWalletTxn.status == "pending",
            ).order_by(StorefrontWalletTxn.created_at)
        )
    ).scalars().all()
    return list(rows)
