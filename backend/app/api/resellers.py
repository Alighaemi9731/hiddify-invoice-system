"""Resellers: list (with panel), detail, edit price / billing exclusion."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_subject
from app.models import Panel, Reseller
from app.schemas.reseller import ResellerOut, ResellerUpdate
from app.services import enforcement, pricing

router = APIRouter(
    prefix="/api/resellers", tags=["resellers"], dependencies=[Depends(get_current_subject)]
)


def _to_out(r: Reseller, panel_key: str, default_price: int) -> ResellerOut:
    return ResellerOut(
        id=r.id, panel_id=r.panel_id, panel_key=panel_key, admin_uuid=r.admin_uuid,
        name=r.name, parent_admin_uuid=r.parent_admin_uuid, mode=r.mode, is_owner=r.is_owner,
        comment=r.comment, exclude_from_billing=r.exclude_from_billing,
        price_per_gb=r.price_per_gb, effective_price_per_gb=(r.price_per_gb or default_price),
        min_sale_toman=r.min_sale_toman,
        bot_chat_id=r.bot_chat_id, panel_telegram_id=r.panel_telegram_id, link_tag=r.link_tag,
        registered=r.bot_chat_id is not None, enforcement_state=r.enforcement_state.value,
        panel_max_users=r.panel_max_users, panel_max_active_users=r.panel_max_active_users,
        last_seen_at=r.last_seen_at,
    )


@router.get("", response_model=list[ResellerOut])
async def list_resellers(
    panel_id: int | None = None,
    q: str | None = Query(None, description="search by name"),
    include_owners: bool = False,
    registered: bool | None = None,
    limit: int = Query(500, le=5000),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> list[ResellerOut]:
    default_price = await pricing.get_default_price_per_gb(session)
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
    rows = (await session.execute(query)).all()
    return [_to_out(r, key, default_price) for r, key in rows]


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

    # Index by (panel_id, admin_uuid); group children by parent.
    by_key: dict[tuple[int, str], tuple[Reseller, str]] = {}
    owner_uuids: set[str] = set()
    children: dict[tuple[int, str], list[tuple[Reseller, str]]] = {}
    for r, key in rows:
        by_key[(r.panel_id, r.admin_uuid)] = (r, key)
        if r.is_owner:
            owner_uuids.add(r.admin_uuid)
    for r, key in rows:
        if r.parent_admin_uuid:
            children.setdefault((r.panel_id, r.parent_admin_uuid), []).append((r, key))

    def node(r: Reseller, key: str) -> dict:
        kids = sorted(children.get((r.panel_id, r.admin_uuid), []), key=lambda x: x[0].name)
        out = _to_out(r, key, default_price).model_dump()
        out["children"] = [node(c, k) for c, k in kids if not c.is_owner]
        out["descendant_count"] = _count(out["children"])
        return out

    # Roots = non-owner resellers whose parent is an Owner (or has no parent in set).
    roots: list[dict] = []
    for r, key in sorted(rows, key=lambda x: x[0].name):
        if r.is_owner:
            continue
        parent_is_owner = r.parent_admin_uuid in owner_uuids
        parent_missing = (r.panel_id, r.parent_admin_uuid) not in by_key
        if parent_is_owner or parent_missing or not r.parent_admin_uuid:
            n = node(r, key)
            if q:
                ql = q.lower()
                # keep the root if it or any descendant matches
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
        r.min_sale_toman = body.min_sale_toman or None
    if body.exclude_from_billing is not None:
        r.exclude_from_billing = body.exclude_from_billing
    await session.commit()
    default_price = await pricing.get_default_price_per_gb(session)
    panel = await session.get(Panel, r.panel_id)
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
        "error": action.error,
    }


@router.post("/{reseller_id}/restore")
async def restore(reseller_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    r = await session.get(Reseller, reseller_id)
    if not r:
        raise HTTPException(404, "Reseller not found")
    action = await enforcement.restore_reseller(session, r)
    if action is None:
        return {"reseller_id": reseller_id, "status": "not_enforced"}
    return {
        "reseller_id": reseller_id, "status": action.status.value,
        "restored_users": action.affected_count, "error": action.error,
    }
