"""Resellers: list (with panel), detail, edit price / billing exclusion."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_subject
from app.models import EndUserSnapshot, Panel, Reseller
from app.schemas.reseller import (
    BumpLimitsBody,
    CanAddAdminBody,
    ResellerOut,
    ResellerUpdate,
)
from app.services import admin_capacity, enforcement, pricing

router = APIRouter(
    prefix="/api/resellers", tags=["resellers"], dependencies=[Depends(get_current_subject)]
)


def _to_out(
    r: Reseller, panel_key: str, default_price: int,
    counts: dict[tuple[int, str], tuple[int, int]] | None = None,
) -> ResellerOut:
    total, active = (counts or {}).get((r.panel_id, r.admin_uuid), (0, 0))
    # Fill % of the admin's user quota — what the capacity column sorts by. No limit → 0
    # so unlimited admins sort as "empty" rather than randomly by raw count.
    cap = (total / r.panel_max_users * 100) if r.panel_max_users else 0.0
    return ResellerOut(
        id=r.id, panel_id=r.panel_id, panel_key=panel_key, admin_uuid=r.admin_uuid,
        name=r.name, parent_admin_uuid=r.parent_admin_uuid, mode=r.mode, is_owner=r.is_owner,
        comment=r.comment, exclude_from_billing=r.exclude_from_billing,
        price_per_gb=r.price_per_gb, effective_price_per_gb=(r.price_per_gb or default_price),
        min_sale_toman=r.min_sale_toman,
        bot_chat_id=r.bot_chat_id, panel_telegram_id=r.panel_telegram_id, link_tag=r.link_tag,
        registered=r.bot_chat_id is not None, enforcement_state=r.enforcement_state.value,
        panel_max_users=r.panel_max_users, panel_max_active_users=r.panel_max_active_users,
        can_add_admin=r.can_add_admin,
        users_count=total, active_users_count=active, capacity_pct=round(cap, 1),
        last_seen_at=r.last_seen_at,
    )


async def _usage_counts(
    session: AsyncSession, panel_id: int | None
) -> dict[tuple[int, str], tuple[int, int]]:
    """(total, active) end-users per creating admin, in one grouped query.
    active = enabled AND is_active (the metric the panel's max_active_users tracks)."""
    total_q = (
        select(EndUserSnapshot.panel_id, EndUserSnapshot.added_by_uuid,
               func.count(EndUserSnapshot.id))
        .where(EndUserSnapshot.added_by_uuid.is_not(None))
        .group_by(EndUserSnapshot.panel_id, EndUserSnapshot.added_by_uuid)
    )
    if panel_id is not None:
        total_q = total_q.where(EndUserSnapshot.panel_id == panel_id)
    out: dict[tuple[int, str], tuple[int, int]] = {}
    for pid, uuid, n in (await session.execute(total_q)).all():
        out[(pid, uuid)] = (int(n), 0)
    active_q = total_q.where(
        EndUserSnapshot.enable.is_(True), EndUserSnapshot.is_active.is_(True)
    )
    for pid, uuid, n in (await session.execute(active_q)).all():
        prev = out.get((pid, uuid), (0, 0))
        out[(pid, uuid)] = (prev[0], int(n))
    return out


@router.get("", response_model=list[ResellerOut])
async def list_resellers(
    panel_id: int | None = None,
    q: str | None = Query(None, description="search by name"),
    include_owners: bool = False,
    registered: bool | None = None,
    top_level_only: bool = Query(
        False, description="only main (top-level) resellers — exclude sub-resellers"
    ),
    limit: int = Query(500, le=5000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[ResellerOut]:
    default_price = await pricing.get_default_price_per_gb(session)
    # When asked for main resellers only, compute the root set (same logic as the tree view)
    # from ALL of the panel's resellers, then keep only those rows below.
    root_keys: set[tuple[int, str]] | None = None
    if top_level_only:
        from app.services.reseller_stats import top_level_roots

        base = select(Reseller)
        if panel_id is not None:
            base = base.where(Reseller.panel_id == panel_id)
        all_res = (await session.execute(base)).scalars().all()
        root_keys = {(r.panel_id, r.admin_uuid) for r in top_level_roots(all_res)}

    query = select(Reseller, Panel.key).join(Panel, Reseller.panel_id == Panel.id)
    if panel_id is not None:
        query = query.where(Reseller.panel_id == panel_id)
    if not include_owners:
        query = query.where(Reseller.is_owner.is_(False))
    if registered is True:
        query = query.where(Reseller.bot_chat_id.is_not(None))
    elif registered is False:
        query = query.where(Reseller.bot_chat_id.is_(None))
    if q:
        query = query.where(or_(Reseller.name.ilike(f"%{q}%"), Reseller.admin_uuid.ilike(f"%{q}%")))
    query = query.order_by(Reseller.name).limit(limit).offset(offset)
    rows = list((await session.execute(query)).tuples().all())
    if root_keys is not None:
        rows = [(r, key) for r, key in rows if (r.panel_id, r.admin_uuid) in root_keys]
    counts = await _usage_counts(session, panel_id)
    return [_to_out(r, key, default_price, counts) for r, key in rows]


@router.get("/tree")
async def reseller_tree(
    panel_id: int | None = None,
    q: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """Top-level resellers, each with its descendant sub-resellers nested under it.

    Lets the panel show who belongs to whom (a sub-reseller appears inside its
    parent admin's group)."""
    default_price = await pricing.get_default_price_per_gb(session)
    query = select(Reseller, Panel.key).join(Panel, Reseller.panel_id == Panel.id)
    if panel_id is not None:
        query = query.where(Reseller.panel_id == panel_id)
    rows = (await session.execute(query)).all()
    counts = await _usage_counts(session, panel_id)

    # Identity is panel-scoped and UUIDs are case-insensitive.
    def identity(r: Reseller) -> tuple[int, str]:
        return (r.panel_id, r.admin_uuid.lower())

    def parent_identity(r: Reseller) -> tuple[int, str] | None:
        return (r.panel_id, r.parent_admin_uuid.lower()) if r.parent_admin_uuid else None

    by_key: dict[tuple[int, str], tuple[Reseller, str]] = {}
    owner_keys: set[tuple[int, str]] = set()
    children: dict[tuple[int, str], list[tuple[Reseller, str]]] = {}
    for r, key in rows:
        by_key[identity(r)] = (r, key)
        if r.is_owner:
            owner_keys.add(identity(r))
    for r, key in rows:
        parent_key = parent_identity(r)
        if parent_key:
            children.setdefault(parent_key, []).append((r, key))

    emitted: set[tuple[int, str]] = set()

    def node(r: Reseller, key: str, ancestors: frozenset[tuple[int, str]]) -> dict:
        current = identity(r)
        emitted.add(current)
        kids = sorted(children.get(current, []), key=lambda x: x[0].name)
        child_nodes: list[dict] = []
        cycle_detected = False
        next_ancestors = ancestors | {current}
        for child, child_panel_key in kids:
            child_key = identity(child)
            if child.is_owner or child_key in next_ancestors or child_key in emitted:
                cycle_detected = True
                continue
            child_nodes.append(node(child, child_panel_key, next_ancestors))
        out = _to_out(r, key, default_price, counts).model_dump()
        out["children"] = child_nodes
        out["descendant_count"] = _count(out["children"])
        out["cycle_detected"] = cycle_detected or any(
            child.get("cycle_detected", False) for child in child_nodes
        )
        return out

    # Roots = non-owner resellers whose parent is an Owner (or has no parent in set).
    roots: list[dict] = []
    for r, key in sorted(rows, key=lambda x: x[0].name):
        if r.is_owner:
            continue
        current = identity(r)
        parent_key = parent_identity(r)
        parent_is_owner = parent_key in owner_keys
        parent_missing = parent_key not in by_key
        if parent_is_owner or parent_missing or not r.parent_admin_uuid:
            if current in emitted:
                continue
            n = node(r, key, frozenset())
            if q:
                ql = q.lower()
                # keep the root if it or any descendant matches
                if ql not in (n["name"] or "").lower() and not _matches(n["children"], ql):
                    continue
            roots.append(n)
    # A malformed cyclic component has no natural root. Surface it once as a synthetic root
    # instead of recursing forever or silently hiding all of its members.
    for r, key in sorted(rows, key=lambda x: x[0].name):
        if not r.is_owner and identity(r) not in emitted:
            n = node(r, key, frozenset())
            if q:
                ql = q.lower()
                if ql not in (n["name"] or "").lower() and not _matches(n["children"], ql):
                    continue
            roots.append(n)
    return roots


def _count(children: list[dict]) -> int:
    return sum(1 + _count(c["children"]) for c in children)


def _matches(children: list[dict], ql: str) -> bool:
    for c in children:
        if ql in (c["name"] or "").lower() or _matches(c["children"], ql):
            return True
    return False


@router.get("/{reseller_id}", response_model=ResellerOut)
async def get_reseller(reseller_id: int, session: AsyncSession = Depends(get_session)) -> ResellerOut:
    default_price = await pricing.get_default_price_per_gb(session)
    r = await session.get(Reseller, reseller_id)
    if not r:
        raise HTTPException(404, "Reseller not found")
    panel = await session.get(Panel, r.panel_id)
    if panel is None:
        raise HTTPException(409, "Reseller references a missing panel")
    return _to_out(r, panel.key, default_price)


@router.patch("/{reseller_id}", response_model=ResellerOut)
async def update_reseller(
    reseller_id: int, body: ResellerUpdate, session: AsyncSession = Depends(get_session)
) -> ResellerOut:
    r = await session.get(Reseller, reseller_id)
    if not r:
        raise HTTPException(404, "Reseller not found")
    if body.price_per_gb is not None:
        r.price_per_gb = body.price_per_gb or None
    if body.min_sale_toman is not None:
        # Keep an explicit 0 ("no minimum-sale floor") as 0 — `or None` used to coerce it to
        # None, silently reverting the reseller to the global default floor.
        r.min_sale_toman = int(body.min_sale_toman)
    if body.exclude_from_billing is not None:
        r.exclude_from_billing = body.exclude_from_billing
    await session.commit()
    default_price = await pricing.get_default_price_per_gb(session)
    panel = await session.get(Panel, r.panel_id)
    if panel is None:
        raise HTTPException(409, "Reseller references a missing panel")
    return _to_out(r, panel.key, default_price)


@router.post("/{reseller_id}/enforce")
async def enforce(
    reseller_id: int, dry_run: bool | None = None, session: AsyncSession = Depends(get_session)
) -> dict:
    r = await session.get(Reseller, reseller_id)
    if not r:
        raise HTTPException(404, "Reseller not found")
    action = await enforcement.enforce_reseller(session, r, dry_run=dry_run)
    return {
        "reseller_id": reseller_id, "status": action.status.value,
        "dry_run": action.dry_run, "affected_users": action.affected_count,
        "queued": action.status.value in ("planned", "partial"),
        "error": action.error,
    }


@router.post("/{reseller_id}/restore")
async def restore(reseller_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    r = await session.get(Reseller, reseller_id)
    if not r:
        raise HTTPException(404, "Reseller not found")
    action = await enforcement.queue_restore(session, r, reason="panel")
    if action is None:
        return {"reseller_id": reseller_id, "status": "not_enforced"}
    return {
        "reseller_id": reseller_id, "status": action.status.value,
        "queued": action.status.value in ("planned", "partial"),
        "restored_users": action.affected_count, "error": action.error,
    }


@router.post("/{reseller_id}/bump-limits")
async def bump_limits(
    reseller_id: int,
    body: BumpLimitsBody | None = Body(None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Add `amount` (default 100) to this admin's max_users AND max_active_users on the panel."""
    r = await session.get(Reseller, reseller_id)
    if not r:
        raise HTTPException(404, "Reseller not found")
    amount = body.amount if body else 100
    try:
        new_mu, new_mau = await admin_capacity.bump_limits(session, r, amount)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"اعمال روی پنل ناموفق بود: {exc}") from exc
    return {
        "reseller_id": reseller_id, "amount": amount,
        "max_users": new_mu, "max_active_users": new_mau,
    }


@router.post("/{reseller_id}/can-add-admin")
async def set_can_add_admin(
    reseller_id: int, body: CanAddAdminBody, session: AsyncSession = Depends(get_session)
) -> dict:
    """Turn this admin's ability to create sub-admins on/off (Hiddify `can_add_admin`)."""
    r = await session.get(Reseller, reseller_id)
    if not r:
        raise HTTPException(404, "Reseller not found")
    try:
        await admin_capacity.set_can_add_admin(session, r, body.enabled)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"اعمال روی پنل ناموفق بود: {exc}") from exc
    return {"reseller_id": reseller_id, "can_add_admin": body.enabled}
