"""One-shot remediation: re-attach plan-less storefront orders to the shop's live twin plan.

Deleting a plan `SET NULL`s every order bought on it. Until v1.123.5 such an order kept renewing at
its purchase-time `order.price_toman` forever — a shop that replaced its plans (delete + recreate at
a new price, the natural way to "change prices" in the bot) kept renewing every existing customer at
the OLD price with no way to reach them (shop 25: 250k plan, customers renewing at 200k). Renewal now
refuses a plan-less order outright (`plan_gone`), so without this sweep those same customers would
simply lose their renew button.

This sweep links each such order to the ONE enabled plan in the same shop with the same gb + days,
so its next renewal is allowed and bills the shop's current price. It never guesses: an order with
no enabled twin stays plan-less (its customer is pointed at the current plans instead), and an order
with SEVERAL twins is reported as ambiguous and left alone — picking one would pick a price.

Owner-run, once, `dry_run` by default:

    python -m app.services.storefront_plan_relink            # report only
    python -m app.services.storefront_plan_relink --apply    # write
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StorefrontCustomer, StorefrontOrder, StorefrontPlan

log = logging.getLogger("storefront.plan_relink")

# Every status that can still renew or be resumed. Terminal rows (deleted/failed) are history.
_LIVE_STATUSES = ("pending", "provisioned", "renewing", "disabled")


async def relink(session: AsyncSession, *, dry_run: bool = True) -> dict:
    """Link plan-less live orders to their shop's unique enabled same-gb/days plan.

    Returns counters plus a per-shop breakdown; writes (and commits) only when `dry_run` is False."""
    orders = (
        await session.execute(
            select(StorefrontOrder, StorefrontCustomer.storefront_bot_id)
            .join(StorefrontCustomer, StorefrontCustomer.id == StorefrontOrder.customer_id)
            .where(
                StorefrontOrder.plan_id.is_(None),
                StorefrontOrder.is_trial.is_(False),
                StorefrontOrder.status.in_(_LIVE_STATUSES),
            )
            .order_by(StorefrontOrder.id)
        )
    ).all()
    shop_ids = sorted({sf_id for _o, sf_id in orders})
    twins: dict[tuple[int, int, int], list[StorefrontPlan]] = defaultdict(list)
    if shop_ids:
        plans = (
            await session.execute(
                select(StorefrontPlan).where(
                    StorefrontPlan.storefront_bot_id.in_(shop_ids),
                    StorefrontPlan.enabled.is_(True),
                )
            )
        ).scalars().all()
        for plan in plans:
            twins[(plan.storefront_bot_id, int(plan.gb), int(plan.days))].append(plan)

    counts = {"candidates": len(orders), "relinked": 0, "price_up": 0, "price_down": 0,
              "price_same": 0, "unmatched": 0, "ambiguous": 0}
    per_shop: dict[int, dict] = defaultdict(lambda: {"relinked": 0, "unmatched": 0, "ambiguous": 0})
    for order, sf_id in orders:
        matches = twins.get((sf_id, int(order.gb), int(order.days)), [])
        if not matches:
            counts["unmatched"] += 1
            per_shop[sf_id]["unmatched"] += 1
            continue
        if len(matches) > 1:
            counts["ambiguous"] += 1
            per_shop[sf_id]["ambiguous"] += 1
            continue
        plan = matches[0]
        new_price, old_price = int(plan.price_toman), int(order.price_toman)
        counts["price_up" if new_price > old_price else
               "price_down" if new_price < old_price else "price_same"] += 1
        counts["relinked"] += 1
        per_shop[sf_id]["relinked"] += 1
        if not dry_run:
            order.plan_id = plan.id
    if dry_run:
        await session.rollback()
    else:
        await session.commit()
        log.info("storefront plan relink applied: %s", counts)
    return {"dry_run": dry_run, **counts, "shops": {str(k): v for k, v in sorted(per_shop.items())}}


async def _main(apply: bool) -> None:
    from app.core.db import SessionLocal

    async with SessionLocal() as session:
        result = await relink(session, dry_run=not apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(_main("--apply" in sys.argv[1:]))
