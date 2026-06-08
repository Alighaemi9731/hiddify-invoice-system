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

from app.models import EndUserSnapshot, Panel, Reseller
from app.services import pricing
from app.services.invoice_engine import (
    BundleResult, build_children_map, collect_descendants, compute_invoices,
)
from app.services.periods import Period, current_month, month_period, today as _today


def _last_months(n: int, today: dt.date | None = None) -> list[Period]:
    today = today or _today()
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
        panel_synced_at=await _panel_synced_at(session, node.panel_id),
    )
    return next((b for b in bundles if b.root.id == node.id), None)


async def node_invoice_own(
    session: AsyncSession, node: Reseller, period: Period
) -> BundleResult | None:
    """Compute an invoice bundle for `node`'s OWN users only — just `node.admin_uuid`, NOT
    its descendants — priced at the node's own price_per_gb. Used so the top reseller's
    interim PDF shows ONLY their own users, mirroring each sub-reseller's separate subtree
    PDF (own + each sub's subtree together cover everyone exactly once). Not persisted.
    Returns None if the node has no billable own usage in the period."""
    users = (
        await session.execute(
            select(EndUserSnapshot).where(
                EndUserSnapshot.panel_id == node.panel_id,
                EndUserSnapshot.added_by_uuid == node.admin_uuid,
            )
        )
    ).scalars().all()
    # Pass [node] alone so it's the single billable root and only its own users are summed.
    bundles = compute_invoices(
        [node], users, period,
        default_price_per_gb=await pricing.get_default_price_per_gb(session),
        excluded_usage_gb=await pricing.get_excluded_usage_gb(session),
        default_min_sale_toman=await pricing.get_default_min_sale(session),
        free_threshold_gb=await pricing.get_free_threshold_gb(session),
        panel_synced_at=await _panel_synced_at(session, node.panel_id),
    )
    return next((b for b in bundles if b.root.id == node.id), None)


async def _panel_synced_at(session: AsyncSession, panel_id: int) -> dt.datetime | None:
    """The panel's latest sync time — a user whose snapshot is older than this is gone from
    the panel, so it's billed on consumption (mirrors the real invoice)."""
    p = await session.get(Panel, panel_id)
    return p.last_synced_at if p else None


def _billable_gb_for_period(
    users, period: Period, free_threshold: float, excluded: set[int] | None = None,
    panel_synced_at: dt.datetime | None = None,
) -> tuple[float, int]:
    """Sum of billable GB (and count) of services created in `period`, using the invoice
    engine's EXACT per-user rule via `billable_gb_for_user` — free threshold, excluded sizes,
    AND consumption-billing for users removed from the panel — so this report/interim/cap math
    matches the real invoice. Pass `panel_synced_at` to enable the deleted-user rule."""
    from app.services.invoice_engine import billable_gb_for_user

    excluded = excluded or set()
    gb = 0.0
    cnt = 0
    for u in users:
        res = billable_gb_for_user(u, period, excluded, free_threshold, panel_synced_at)
        if res is None:
            continue
        gb += res[0]
        cnt += 1
    return round(gb, 2), cnt


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
    excluded = await pricing.get_excluded_usage_gb(session)
    psa = await _panel_synced_at(session, reseller.panel_id)

    by_month = []
    for p in _last_months(months):
        gb, cnt = _billable_gb_for_period(users, p, free_threshold, excluded, psa)
        by_month.append({
            "label": p.label,
            "gb": gb,
            "amount_toman": round(gb * price),
            "new_services": cnt,
        })

    # Current-month progress against the parent-set GB cap (the bot shows this so the
    # parent can see how much of the sub's monthly quota is used).
    this_period = current_month()
    used_gb = next((m["gb"] for m in by_month if m["label"] == this_period.label), 0.0)
    cap = int(reseller.gb_cap or 0)

    return {
        "name": reseller.name,
        "admin_uuid": reseller.admin_uuid,
        "sub_count": max(0, len(descendants) - 1),
        "total_users": len(users),
        "enabled_users": sum(1 for u in users if u.enable),
        "price_per_gb": price,
        "enforcement_state": reseller.enforcement_state.value,
        "months": by_month,
        "gb_cap": cap,
        "current_period": this_period.label,
        "current_gb": used_gb,
        "cap_pct": (round(used_gb / cap * 100) if cap > 0 else 0),
        "cap_remaining_gb": (round(cap - used_gb, 2) if cap > 0 else None),
    }


async def interim_breakdown(session: AsyncSession, reseller: Reseller, period: Period) -> dict:
    """Interim (so-far-this-period) breakdown for a reseller, split into:
      - own:  the reseller's OWN users (just their admin_uuid)
      - subs: each DIRECT sub-reseller's WHOLE subtree (so every user is counted once,
              under the top sub it rolls up to)
    ALL priced at the MAIN reseller's price_per_gb (a single unit price across the bundle),
    so the totals exactly match the real end-of-month invoice for this node.

    Returns: {price, own:{gb,users,amount}, subs:[{name,gb,users,amount}], total_gb,
    total_amount, total_users}.
    """
    default_price = await pricing.get_default_price_per_gb(session)
    price = int(reseller.price_per_gb or default_price)
    free_threshold = await pricing.get_free_threshold_gb(session)
    excluded = await pricing.get_excluded_usage_gb(session)
    psa = await _panel_synced_at(session, reseller.panel_id)

    # All resellers on the panel → children map, to find this node's DIRECT subs + subtrees.
    panel_resellers = (
        await session.execute(select(Reseller).where(Reseller.panel_id == reseller.panel_id))
    ).scalars().all()
    children = build_children_map(panel_resellers)

    async def _users_for(uuids: set[str]):
        if not uuids:
            return []
        return (
            await session.execute(
                select(EndUserSnapshot).where(
                    EndUserSnapshot.panel_id == reseller.panel_id,
                    EndUserSnapshot.added_by_uuid.in_(uuids),
                )
            )
        ).scalars().all()

    # Own users: exactly this reseller's admin_uuid (NOT descendants).
    own_users = await _users_for({reseller.admin_uuid})
    own_gb, own_cnt = _billable_gb_for_period(own_users, period, free_threshold, excluded, psa)

    # Each direct sub-reseller, counted as its whole subtree (so no user is double-counted).
    subs_out: list[dict] = []
    for child in children.get(reseller.admin_uuid, []):
        subtree = collect_descendants(child, children)
        sub_uuids = {d.admin_uuid for d in subtree}
        sub_users = await _users_for(sub_uuids)
        sgb, scnt = _billable_gb_for_period(sub_users, period, free_threshold, excluded, psa)
        if scnt == 0 and sgb == 0:
            continue  # skip sub-resellers with no sales this period (keeps the report tidy)
        subs_out.append({
            "id": child.id,
            "name": child.name,
            "gb": sgb,
            "users": scnt,
            "amount": round(sgb * price),
        })
    subs_out.sort(key=lambda s: s["gb"], reverse=True)

    total_gb = round(own_gb + sum(s["gb"] for s in subs_out), 2)
    total_users = own_cnt + sum(s["users"] for s in subs_out)
    return {
        "price": price,
        "own": {"gb": own_gb, "users": own_cnt, "amount": round(own_gb * price)},
        "subs": subs_out,
        "total_gb": total_gb,
        "total_users": total_users,
        "total_amount": round(total_gb * price),
        "period": period.label,
    }


async def current_billable_gb(session: AsyncSession, reseller: Reseller) -> float:
    """The sub-tree's billable GB for the CURRENT billing month (for cap checks) — matches the
    real invoice: sold quota for live users, consumption for users removed from the panel."""
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
    free_threshold = await pricing.get_free_threshold_gb(session)
    excluded = await pricing.get_excluded_usage_gb(session)
    psa = await _panel_synced_at(session, reseller.panel_id)
    gb, _ = _billable_gb_for_period(users, current_month(), free_threshold, excluded, psa)
    return gb
