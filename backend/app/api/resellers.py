"""Resellers: list (with panel), detail, edit price / billing exclusion."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict
from types import SimpleNamespace

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import case, func, or_, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_subject
from app.models import (
    BotUser,
    EndUserSnapshot,
    Invoice,
    InvoiceLine,
    Panel,
    Payment,
    Reseller,
    UsageMeter,
)
from app.models.enums import InvoiceStatus, PanelStatus
from app.schemas.reseller import (
    AbsentResellerOut,
    BumpLimitsBody,
    CanAddAdminBody,
    ResellerOut,
    ResellerUpdate,
)
from app.services import admin_capacity, enforcement, payment_guard, pricing
from app.services.invoice_engine import build_children_map
from app.services.presence import snapshot_present_filter

# Resellers removed from Hiddify keep their DB row for billing history but must not
# appear in the UI.  This filter, applied to queries that JOIN Panel, keeps only
# resellers seen in the panel's latest successful sync (or when we have no good sync yet).
_PRESENCE_SKEW = dt.timedelta(seconds=2)


def _present_filter():
    """SQLAlchemy WHERE clause: reseller is still present on the panel."""
    return or_(
        Panel.status != PanelStatus.ok,
        Panel.last_synced_at.is_(None),
        Reseller.last_seen_at.is_(None),
        Reseller.last_seen_at >= Panel.last_synced_at - _PRESENCE_SKEW,
    )


def _is_absent(reseller: Reseller, panel: Panel | None) -> bool:
    """Re-check (in Python) whether a reseller is currently absent — used to guard deletion so an
    active reseller can never be removed through the absent-only path."""
    if panel is None or panel.status != PanelStatus.ok or panel.last_synced_at is None:
        return False
    if reseller.last_seen_at is None:
        return False
    return reseller.last_seen_at < panel.last_synced_at - _PRESENCE_SKEW


router = APIRouter(
    prefix="/api/resellers", tags=["resellers"], dependencies=[Depends(get_current_subject)]
)


def _to_out(
    r: Reseller, panel_key: str, default_price: int,
    counts: dict[tuple[int, str], tuple[int, int]] | None = None,
    usernames: dict[int, str] | None = None,
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
        username=(usernames or {}).get(r.bot_chat_id) if r.bot_chat_id else None,
        registered=r.bot_chat_id is not None, enforcement_state=r.enforcement_state.value,
        panel_max_users=r.panel_max_users, panel_max_active_users=r.panel_max_active_users,
        can_add_admin=r.can_add_admin,
        storefront_enabled=r.storefront_enabled,
        storefront_monthly_fee_toman=r.storefront_monthly_fee_toman,
        users_count=total, active_users_count=active, capacity_pct=round(cap, 1),
        last_seen_at=r.last_seen_at,
    )


def _subtree_counts(
    nodes: list, own_total: dict[tuple[int, str], int],
    own_active: dict[tuple[int, str], int], panel_id: int,
) -> tuple[dict[str, int], dict[str, int]]:
    """Memoized post-order sums of (own + ALL descendants) per node, keyed by lowercased uuid.
    Cycle-safe (a back-edge counts own-only and stops). O(n) — each node's subtree is computed once."""
    children = build_children_map(nodes)
    memo_t: dict[str, int] = {}
    memo_a: dict[str, int] = {}
    visiting: set[str] = set()

    def dfs(u: str) -> tuple[int, int]:
        if u in memo_t:
            return memo_t[u], memo_a[u]
        if u in visiting:  # pathological cycle → count this node's own users only, don't recurse
            return own_total.get((panel_id, u), 0), own_active.get((panel_id, u), 0)
        visiting.add(u)
        t = own_total.get((panel_id, u), 0)
        a = own_active.get((panel_id, u), 0)
        for c in children.get(u, []):
            ct, ca = dfs(c.admin_uuid)
            t += ct
            a += ca
        visiting.discard(u)
        memo_t[u] = t
        memo_a[u] = a
        return t, a

    for n in nodes:
        dfs(n.admin_uuid)
    return memo_t, memo_a


async def _usage_counts(
    session: AsyncSession, panel_id: int | None
) -> dict[tuple[int, str], tuple[int, int]]:
    """(total, active) end-users for each admin's WHOLE SUBTREE (itself + all descendant
    sub-resellers), keyed by (panel_id, admin_uuid). total = all snapshots, active = enabled AND
    is_active. This mirrors what Hiddify shows as an admin's capacity (own + subs), compared
    against the admin's own max_users — so a full subtree can read >100% (red), which is correct.
    UUID matching is case-insensitive throughout (project-wide identity rule).

    Counts only users present in the panel's latest sync — same operational semantics as the
    creation guard in `usercreate.current_user_count`, so the «پُری ظرفیت» bar the owner reads and
    the capacity the reseller can actually use never disagree. (The two still differ in SCOPE by
    design: this one is subtree-wide, the guard is own-users-only.) Snapshots retained for billing
    after deletion occupy no panel slot and so are not counted here."""
    # 1) Own counts per creating admin (case-insensitive uuid key).
    base_q = (
        select(EndUserSnapshot.panel_id, func.lower(EndUserSnapshot.added_by_uuid),
               func.count(EndUserSnapshot.id))
        .join(Panel, Panel.id == EndUserSnapshot.panel_id)
        .where(EndUserSnapshot.added_by_uuid.is_not(None), snapshot_present_filter())
        .group_by(EndUserSnapshot.panel_id, func.lower(EndUserSnapshot.added_by_uuid))
    )
    if panel_id is not None:
        base_q = base_q.where(EndUserSnapshot.panel_id == panel_id)
    own_total: dict[tuple[int, str], int] = {}
    own_active: dict[tuple[int, str], int] = {}
    for pid, uuid, n in (await session.execute(base_q)).all():
        own_total[(pid, uuid)] = own_total.get((pid, uuid), 0) + int(n)
    for pid, uuid, n in (await session.execute(
        base_q.where(EndUserSnapshot.enable.is_(True), EndUserSnapshot.is_active.is_(True))
    )).all():
        own_active[(pid, uuid)] = own_active.get((pid, uuid), 0) + int(n)

    # 2) Reseller hierarchy, built PER PANEL (a uuid may exist on two panels), case-insensitive.
    res_q = select(Reseller.panel_id, Reseller.admin_uuid, Reseller.parent_admin_uuid)
    if panel_id is not None:
        res_q = res_q.where(Reseller.panel_id == panel_id)
    nodes_by_panel: dict[int, list] = defaultdict(list)
    originals_by_panel: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for pid, uuid, parent in (await session.execute(res_q)).all():
        low = (uuid or "").lower()
        nodes_by_panel[pid].append(SimpleNamespace(
            admin_uuid=low, parent_admin_uuid=((parent or "").lower() or None)))
        originals_by_panel[pid].append((uuid, low))

    # 3) Sum each subtree; key the output by the reseller's ORIGINAL-case uuid so _to_out finds it.
    out: dict[tuple[int, str], tuple[int, int]] = {}
    for pid, nodes in nodes_by_panel.items():
        memo_t, memo_a = _subtree_counts(nodes, own_total, own_active, pid)
        for orig, low in originals_by_panel[pid]:
            out[(pid, orig)] = (memo_t.get(low, 0), memo_a.get(low, 0))
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

        base = select(Reseller).join(Panel, Reseller.panel_id == Panel.id).where(_present_filter())
        if panel_id is not None:
            base = base.where(Reseller.panel_id == panel_id)
        all_res = (await session.execute(base)).scalars().all()
        root_keys = {(r.panel_id, r.admin_uuid) for r in top_level_roots(all_res)}

    query = select(Reseller, Panel.key).join(Panel, Reseller.panel_id == Panel.id).where(_present_filter())
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
        # Relevance ordering so a search surfaces the intended reseller even when dozens share
        # the term (e.g. "ali" matches 50+ names): EXACT name match first, then names that
        # START WITH the term, then everything else — all case-insensitive — then alphabetically.
        # Without this, `ORDER BY name` alone buried the exact match past the `limit` window, so
        # searching "ali" never returned the reseller literally named "ali".
        ql = q.strip().lower()
        rank = case(
            (func.lower(Reseller.name) == ql, 0),
            (func.lower(Reseller.name).like(f"{ql}%"), 1),
            else_=2,
        )
        query = query.order_by(rank, Reseller.name).limit(limit).offset(offset)
    else:
        query = query.order_by(Reseller.name).limit(limit).offset(offset)
    rows = list((await session.execute(query)).tuples().all())
    if root_keys is not None:
        rows = [(r, key) for r, key in rows if (r.panel_id, r.admin_uuid) in root_keys]
    counts = await _usage_counts(session, panel_id)
    # Telegram @usernames for the registered resellers in this page → clickable PV deep-link.
    chat_ids = [r.bot_chat_id for r, _ in rows if r.bot_chat_id]
    usernames: dict[int, str] = {}
    if chat_ids:
        usernames = {
            tid: uname
            for tid, uname in (
                await session.execute(
                    select(BotUser.telegram_id, BotUser.username).where(
                        BotUser.telegram_id.in_(chat_ids), BotUser.username.is_not(None)
                    )
                )
            ).all()
        }
    return [_to_out(r, key, default_price, counts, usernames) for r, key in rows]


@router.get("/absent", response_model=list[AbsentResellerOut])
async def list_absent_resellers(
    panel_id: int | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[AbsentResellerOut]:
    """Resellers whose admin was removed from Hiddify but whose DB row still lingers — the
    candidates for safe deletion. Owners are excluded (sync removes absent owner rows itself).

    Restricts to panels with a good latest sync (status ok + a real last_synced_at) so a
    failed/never-synced panel never makes everyone look 'absent'. The precise skew check is done
    in Python (`_is_absent`) so it behaves identically on Postgres and SQLite (the DB-side
    `timestamp - interval` arithmetic is Postgres-only)."""
    query = (
        select(Reseller, Panel)
        .join(Panel, Reseller.panel_id == Panel.id)
        .where(
            Panel.status == PanelStatus.ok,
            Panel.last_synced_at.is_not(None),
            Reseller.last_seen_at.is_not(None),
            Reseller.is_owner.is_(False),
        )
    )
    if panel_id is not None:
        query = query.where(Reseller.panel_id == panel_id)
    candidates = [(r, p) for r, p in (await session.execute(query)).tuples().all()
                  if _is_absent(r, p)]
    # All candidates have a non-null last_seen_at (SQL filter), so this never mixes None.
    candidates.sort(key=lambda rp: rp[0].last_seen_at)  # type: ignore[arg-type,return-value]
    rows = [(r, p.key) for r, p in candidates]
    if not rows:
        return []

    counts = await _usage_counts(session, panel_id)
    ids = [r.id for r, _ in rows]
    # Direct children still lingering in the DB, per (panel_id, parent_admin_uuid).
    child_counts: dict[tuple[int, str], int] = {}
    for pid, parent, n in (await session.execute(
        select(Reseller.panel_id, Reseller.parent_admin_uuid, func.count())
        .where(Reseller.parent_admin_uuid.is_not(None))
        .group_by(Reseller.panel_id, Reseller.parent_admin_uuid)
    )).all():
        child_counts[(pid, parent)] = int(n)
    # Which absentees have delivered/paid invoices, and which have any payment.
    nondraft = set((await session.execute(
        select(Invoice.reseller_id).where(
            Invoice.reseller_id.in_(ids), Invoice.status != InvoiceStatus.draft
        ).distinct()
    )).scalars().all())
    with_pay = set((await session.execute(
        select(Payment.reseller_id).where(Payment.reseller_id.in_(ids)).distinct()
    )).scalars().all())

    out: list[AbsentResellerOut] = []
    for r, key in rows:
        total, _active = counts.get((r.panel_id, r.admin_uuid), (0, 0))
        out.append(AbsentResellerOut(
            id=r.id, panel_id=r.panel_id, panel_key=key, admin_uuid=r.admin_uuid,
            name=r.name, last_seen_at=r.last_seen_at, users_count=total,
            sub_resellers=child_counts.get((r.panel_id, r.admin_uuid), 0),
            has_nondraft_invoices=r.id in nondraft, has_payments=r.id in with_pay,
        ))
    return out


@router.delete("/{reseller_id}/absent")
async def delete_absent_reseller(
    reseller_id: int, force: bool = False, session: AsyncSession = Depends(get_session)
) -> dict:
    """Permanently delete an ABSENT reseller, the ABSENT sub-resellers beneath it, and the end-user
    snapshots (+ usage meters) those removed admins created — full cleanup of a branch that's gone
    from the panel. PRESENT sub-resellers are left untouched (the next sync would just recreate
    them). The durable financial ledger (`financial_records`, no FK) is intentionally KEPT.
    Guarded: a reseller currently present on the panel is refused (this path is removed-admins
    only)."""
    reseller = await session.get(Reseller, reseller_id)
    if reseller is None:
        raise HTTPException(404, "Reseller not found")
    panel = await session.get(Panel, reseller.panel_id)
    if not _is_absent(reseller, panel):
        raise HTTPException(
            409, "این نماینده هنوز روی پنل حاضر است؛ این مسیر فقط برای حذفِ نماینده‌های حذف‌شده از پنل است.")

    # Walk the whole subtree on this panel (case-insensitive parent links, cycle-safe). Delete the
    # target + every ABSENT descendant; traverse THROUGH present nodes to still reach absent ones
    # below them, but never delete a present node.
    panel_resellers = (await session.execute(
        select(Reseller).where(Reseller.panel_id == reseller.panel_id)
    )).scalars().all()
    children: dict[str, list[Reseller]] = {}
    for r in panel_resellers:
        if r.parent_admin_uuid:
            children.setdefault(r.parent_admin_uuid.lower(), []).append(r)

    to_delete = [reseller]
    seen_ids = {reseller.id}
    queue = [reseller]
    while queue:
        cur = queue.pop()
        for child in children.get((cur.admin_uuid or "").lower(), []):
            if child.id in seen_ids:
                continue
            seen_ids.add(child.id)
            queue.append(child)                 # descend through present nodes too
            if _is_absent(child, panel):
                to_delete.append(child)

    del_ids = [r.id for r in to_delete]
    del_uuids = [(r.admin_uuid or "").lower() for r in to_delete]

    # A payment can settle invoices belonging to SEVERAL resellers (one transfer, several panels).
    # Deleting one of them would destroy that payment and leave the others' invoices `paid` with no
    # evidence. Refuse unless the owner explicitly forces it, and archive the ledger first if so.
    blocking = await payment_guard.blocking_payments(session, set(del_ids))
    if blocking and not force:
        raise HTTPException(409, payment_guard.refusal_message(blocking))
    if blocking:
        await payment_guard.archive_before_delete(session, blocking)

    # End-users created by any of the deleted admins on this panel (their "users").
    user_uuids = list((await session.execute(
        select(EndUserSnapshot.user_uuid).where(
            EndUserSnapshot.panel_id == reseller.panel_id,
            func.lower(EndUserSnapshot.added_by_uuid).in_(del_uuids),
        )
    )).scalars().all())

    # Order matters on SQLite (no FK enforcement): children before parents.
    if user_uuids:
        await session.execute(sa_delete(UsageMeter).where(
            UsageMeter.panel_id == reseller.panel_id, UsageMeter.user_uuid.in_(user_uuids)))
    await session.execute(sa_delete(EndUserSnapshot).where(
        EndUserSnapshot.panel_id == reseller.panel_id,
        func.lower(EndUserSnapshot.added_by_uuid).in_(del_uuids)))
    await session.execute(sa_delete(Payment).where(Payment.reseller_id.in_(del_ids)))
    inv_ids = list((await session.execute(
        select(Invoice.id).where(Invoice.reseller_id.in_(del_ids))
    )).scalars().all())
    if inv_ids:
        # Keep `settled_invoice_ids` in step with the cascading `payment_settlements` mirror, so a
        # surviving payment never names an invoice that no longer exists.
        await payment_guard.prune_deleted_invoice_ids(session, set(inv_ids))
        await session.execute(sa_delete(InvoiceLine).where(InvoiceLine.invoice_id.in_(inv_ids)))
        await session.execute(sa_delete(Invoice).where(Invoice.id.in_(inv_ids)))
    await session.execute(sa_delete(Reseller).where(Reseller.id.in_(del_ids)))
    await session.commit()
    return {
        "deleted": True,
        "reseller_id": reseller_id,
        "resellers_deleted": len(del_ids),   # target + absent sub-resellers
        "users_deleted": len(user_uuids),    # their end-user snapshots
        "financial_records_kept": True,      # the durable ledger is never touched by this delete
    }


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
    query = (
        select(Reseller, Panel.key)
        .join(Panel, Reseller.panel_id == Panel.id)
        .where(_present_filter())
    )
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
    # Gate on PRESENCE (model_fields_set), not truthiness: the edit dialog sends explicit
    # JSON null to CLEAR a per-reseller override back to the global default. `is not None`
    # couldn't tell an explicit null from an absent field, so «خالی = پیش‌فرض» silently did
    # nothing (a stale price/min-sale/fee override survived, with a false success toast).
    data = body.model_dump(exclude_unset=True)
    if "price_per_gb" in data:
        # null OR 0 → clear to the global default; a positive value overrides.
        r.price_per_gb = data["price_per_gb"] or None
    if "min_sale_toman" in data:
        # null → clear to the global default; 0 is the distinct "no floor" state; >0 overrides.
        r.min_sale_toman = data["min_sale_toman"]
    if "storefront_monthly_fee_toman" in data:
        # null OR 0 → use the global default fee (NULL); a positive value overrides.
        r.storefront_monthly_fee_toman = data["storefront_monthly_fee_toman"] or None
    if data.get("exclude_from_billing") is not None:
        # «معاف از فاکتور» only makes sense on a TOP-LEVEL reseller — a sub is billed via its
        # parent's bundle, so the flag on a sub is silently ignored by the engine. Reject
        # setting it True on a non-root so the UI can't imply an effect it doesn't have.
        if data["exclude_from_billing"] and not r.exclude_from_billing:
            from app.services.reseller_stats import top_level_roots

            siblings = (
                await session.execute(select(Reseller).where(Reseller.panel_id == r.panel_id))
            ).scalars().all()
            root_ids = {x.id for x in top_level_roots(siblings)}
            if r.id not in root_ids:
                raise HTTPException(
                    409,
                    "«معاف از فاکتور» فقط برای نماینده‌های سطح‌اول معنا دارد؛ زیرمجموعه از طریق "
                    "نمایندهٔ بالادستی‌اش محاسبه می‌شود.",
                )
        r.exclude_from_billing = data["exclude_from_billing"]
    if data.get("storefront_enabled") is not None:
        r.storefront_enabled = data["storefront_enabled"]
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


@router.post("/{reseller_id}/unbind-telegram")
async def unbind_telegram(
    reseller_id: int, session: AsyncSession = Depends(get_session)
) -> dict:
    """Release this reseller's Telegram binding so they can re-register from a NEW account.

    Owner-only escape hatch for when a reseller loses access to the Telegram account they
    registered with (bot registration otherwise refuses to re-bind a row already tied to a
    different chat). Clears exactly what the bot's own `/removelink` clears — bot_chat_id,
    link_tag, registered_at — for THIS reseller row only. DB-only: nothing on the Hiddify panel
    changes. Idempotent."""
    r = await session.get(Reseller, reseller_id)
    if not r:
        raise HTTPException(404, "Reseller not found")
    r.bot_chat_id = None
    r.link_tag = None
    r.registered_at = None
    await session.commit()
    return {"reseller_id": reseller_id, "name": r.name, "registered": False}
