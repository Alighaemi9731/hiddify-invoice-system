"""
Headline numbers for a reseller/sub-reseller node, used by the bot's sub-reseller
management view. Sub-resellers are folded into their parent's invoice, so they have
no Invoice rows of their own — we compute their sales straight from the end-user
snapshots, the same way the invoice engine does for a bundle.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EndUserSnapshot, Reseller
from app.services import pricing
from app.services.invoice_engine import (
    BundleResult, build_children_map, collect_descendants, compute_invoices,
)
from app.services.periods import Period, month_period


def _last_months(n: int, today: dt.date | None = None) -> list[Period]:
    today = today or dt.date.today()
    out: list[Period] = []
    y, m = today.year, today.month
    for _ in range(n):
        out.append(month_period(y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out


async def node_descendants(session: AsyncSession, reseller: Reseller) -> list[Reseller]:
    """The reseller node + all of its descendant sub-resellers (same panel)."""
    panel_resellers = (
        await session.execute(select(Reseller).where(Reseller.panel_id == reseller.panel_id))
    ).scalars().all()
    children = build_children_map(panel_resellers)
    return collect_descendants(reseller, children)


async def node_invoice(
    session: AsyncSession, node: Reseller, period: Period
) -> BundleResult | None:
    """Compute an invoice bundle ROOTED at `node` for a period — node + its sub-resellers,
    priced at the node's own price_per_gb. Used so a reseller can bill each of their
    sub-resellers separately. Not persisted (the owner's invoice already covers the whole
    subtree); rendered on demand."""
    descendants = await node_descendants(session, node)
    uuids = {d.admin_uuid for d in descendants}
    users = (
        await session.execute(
            select(EndUserSnapshot).where(
                EndUserSnapshot.panel_id == node.panel_id,
                EndUserSnapshot.added_by_uuid.in_(uuids),
            )
        )
    ).scalars().all()
    # Pass only the subtree (no owner) so `node` is treated as the single billable root.
    bundles = compute_invoices(
        descendants, users, period,
        default_price_per_gb=await pricing.get_default_price_per_gb(session),
        excluded_usage_gb=await pricing.get_excluded_usage_gb(session),
        default_min_sale_toman=await pricing.get_default_min_sale(session),
        free_threshold_gb=await pricing.get_free_threshold_gb(session),
    )
    return next((b for b in bundles if b.root.id == node.id), None)


async def node_report(session: AsyncSession, reseller: Reseller, *, months: int = 3) -> dict:
    descendants = await node_descendants(session, reseller)
    uuids = {d.admin_uuid for d in descendants}
    users = (
        await session.execute(
            select(EndUserSnapshot).where(
                EndUserSnapshot.panel_id == reseller.panel_id,
                EndUserSnapshot.added_by_uuid.in_(uuids),
            )
        )
    ).scalars().all()

    default_price = await pricing.get_default_price_per_gb(session)
    price = int(reseller.price_per_gb or default_price)
    free_threshold = await pricing.get_free_threshold_gb(session)

    by_month = []
    for p in _last_months(months):
        gb = 0.0
        cnt = 0
        for u in users:
            if not p.contains(u.start_date):
                continue
            g = float(u.usage_limit_gb or 0)
            if g <= free_threshold + 1e-9:
                continue
            gb += g
            cnt += 1
        by_month.append({
            "label": p.label,
            "gb": round(gb, 2),
            "amount_toman": round(gb * price),
            "new_services": cnt,
        })

    return {
        "name": reseller.name,
        "admin_uuid": reseller.admin_uuid,
        "sub_count": max(0, len(descendants) - 1),
        "total_users": len(users),
        "enabled_users": sum(1 for u in users if u.enable),
        "price_per_gb": price,
        "enforcement_state": reseller.enforcement_state.value,
        "months": by_month,
    }
