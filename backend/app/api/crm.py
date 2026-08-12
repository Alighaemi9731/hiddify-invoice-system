"""
Reseller follow-up board («پیگیری») — churn segmentation plus a snooze-able work queue.

Read endpoints are derived from `app.services.crm`; the write endpoints only ever record
that the owner followed someone up. **Nothing here sends a message.** The owner DMs the
reseller in Telegram themselves; the system's job is to remember who has already been
chased so the same person does not resurface on the next pass.
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_subject
from app.models import Panel, Reseller, ResellerCrmState, ResellerFollowup
from app.schemas.crm import (
    BulkFollowupBody,
    CrmBoardRow,
    CrmFollowupRow,
    CrmMonthPoint,
    CrmResellerDetail,
    CrmSummary,
    FollowupBody,
    FollowupResult,
)
from app.services import crm
from app.services.periods import current_month, today

router = APIRouter(prefix="/api/crm", tags=["crm"], dependencies=[Depends(get_current_subject)])

# The inline sparkline in the table — the drawer shows all `crm.HISTORY_MONTHS`.
TREND_POINTS = 6

_SORTS = {
    "value": lambda r: r.value_at_risk_toman,
    "days": lambda r: (r.days_since_last_sale if r.days_since_last_sale is not None else 10**6),
    "debt": lambda r: r.outstanding_toman,
    "mtd": lambda r: r.mtd_gb,
    "touch": lambda r: (r.last_touch_at.timestamp() if r.last_touch_at else 0.0),
    "name": lambda r: r.reseller_name,
}


def _row(
    m: crm.RootMetrics, segment: str, state: ResellerCrmState | None, day: dt.date
) -> CrmBoardRow:
    return CrmBoardRow(
        reseller_id=m.reseller_id,
        reseller_name=m.name,
        admin_uuid=m.admin_uuid,
        panel_id=m.panel_id,
        panel_key=m.panel_key,
        segment=segment,
        sub_resellers=m.sub_reseller_count,
        registered=m.bot_chat_id is not None,
        value_at_risk_toman=m.value_at_risk_toman,
        mtd_services=m.mtd_services,
        mtd_gb=m.mtd_gb,
        projected_gb=m.projected_gb,
        avg_prev_gb=m.avg_prev_gb,
        last_sale_date=m.last_sale_date,
        days_since_last_sale=m.days_since_last_sale,
        account_age_days=m.account_age_days,
        outstanding_toman=m.outstanding_toman,
        outstanding_count=m.outstanding_count,
        oldest_unpaid_period=m.oldest_unpaid_period,
        last_touch_at=state.last_touch_at if state else None,
        touch_count=state.touch_count if state else 0,
        snoozed_until=state.snoozed_until if state else None,
        muted=bool(state.muted) if state else False,
        note=(state.note or "") if state else "",
        due=crm.is_due(state, day),
        trend_gb=[float(s["gb"]) for s in m.months[-TREND_POINTS:]],
    )


async def _board(session: AsyncSession, day: dt.date) -> list[tuple[crm.RootMetrics, str, ResellerCrmState | None]]:
    """Every eligible reseller with its segment and follow-up state.

    Metrics come from the TTL cache; state and segment are recomputed every call so a
    just-logged follow-up removes the row from the "due" view immediately.
    """
    metrics = await crm.load_board_metrics(session, today_=day)
    thresholds = await crm.load_thresholds(session)
    states = await crm.load_states(session, (m.reseller_id for m in metrics))
    elapsed = max(1, (day - current_month(day).start).days + 1)
    return [
        (m, crm.classify(m, thresholds, elapsed_days=elapsed), states.get(m.reseller_id))
        for m in metrics
    ]


@router.get("/board", response_model=list[CrmBoardRow])
async def board(
    response: Response,
    segment: str | None = None,
    panel_id: int | None = None,
    q: str | None = None,
    view: str = Query("due", pattern="^(due|all|snoozed)$"),
    sort: str = Query("value", pattern="^(value|days|debt|mtd|touch|name)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[CrmBoardRow]:
    """The work queue. `view=due` (default) hides muted resellers and anyone snoozed to a
    future date — that is the whole anti-duplicate mechanism. The filtered total is returned
    in `X-Total-Count`; the population is ~400 rows, so filtering and sorting happen in
    Python over the cached metric bundle rather than in SQL."""
    day = today()
    rows = [_row(m, seg, st, day) for m, seg, st in await _board(session, day)]

    if view == "due":
        rows = [r for r in rows if r.due]
    elif view == "snoozed":
        rows = [r for r in rows if not r.due]
    if segment:
        wanted = {s for s in segment.split(",") if s}
        rows = [r for r in rows if r.segment in wanted]
    if panel_id is not None:
        rows = [r for r in rows if r.panel_id == panel_id]
    if q:
        needle = q.strip().lower()
        rows = [
            r for r in rows
            if needle in r.reseller_name.lower() or needle in r.admin_uuid.lower()
        ]

    rows.sort(key=_SORTS[sort], reverse=(order == "desc"))
    response.headers["X-Total-Count"] = str(len(rows))
    return rows[offset : offset + limit]


@router.get("/summary", response_model=CrmSummary)
async def summary(session: AsyncSession = Depends(get_session)) -> CrmSummary:
    """Per-segment counts over the whole population (not the current page) plus the queue
    sizes the header cards show."""
    day = today()
    triples = await _board(session, day)
    states = [st for _, _, st in triples]
    return CrmSummary(
        counts=crm.segment_counts(seg for _, seg, _ in triples),
        total=len(triples),
        due=sum(1 for st in states if crm.is_due(st, day)),
        snoozed=sum(
            1 for st in states
            if st is not None and not st.muted and st.snoozed_until and st.snoozed_until >= day
        ),
        muted=sum(1 for st in states if st is not None and st.muted),
        snooze_default_days=(await crm.load_thresholds(session)).snooze_default_days,
        generated_at=dt.datetime.now(dt.UTC),
    )


def _followup_row(f: ResellerFollowup) -> CrmFollowupRow:
    return CrmFollowupRow(
        id=f.id,
        reseller_id=f.reseller_id,
        reseller_name=f.reseller_name,
        reseller_admin_uuid=f.reseller_admin_uuid,
        panel_key=f.panel_key,
        segment=f.segment,
        note=f.note or "",
        snoozed_until=f.snoozed_until,
        muted=bool(f.muted),
        actor=f.actor or "",
        created_at=f.created_at,
    )


@router.get("/reseller/{reseller_id}", response_model=CrmResellerDetail)
async def reseller_detail(
    reseller_id: int, session: AsyncSession = Depends(get_session)
) -> CrmResellerDetail:
    """One reseller's card: the board row, the full monthly history for the chart, and every
    follow-up ever logged for them."""
    day = today()
    found = next(
        ((m, seg, st) for m, seg, st in await _board(session, day) if m.reseller_id == reseller_id),
        None,
    )
    if found is None:
        raise HTTPException(status_code=404, detail="reseller not found on the follow-up board")
    m, seg, st = found
    history = (
        await session.execute(
            select(ResellerFollowup)
            .where(ResellerFollowup.reseller_id == reseller_id)
            .order_by(ResellerFollowup.created_at.desc(), ResellerFollowup.id.desc())
            .limit(100)
        )
    ).scalars().all()
    return CrmResellerDetail(
        row=_row(m, seg, st, day),
        months=[CrmMonthPoint(**p) for p in m.months],
        followups=[_followup_row(f) for f in history],
    )


async def _resolve_snooze(
    session: AsyncSession, body: FollowupBody, day: dt.date
) -> dt.date | None:
    """Muting outranks any date. Otherwise: explicit days → explicit date → owner default;
    an explicit 0 days means "leave it on the list"."""
    if body.muted:
        return None
    if body.snooze_days is not None:
        return day + dt.timedelta(days=body.snooze_days) if body.snooze_days > 0 else None
    if body.snooze_until is not None:
        return body.snooze_until
    thresholds = await crm.load_thresholds(session)
    return day + dt.timedelta(days=thresholds.snooze_default_days)


async def _log_followup(
    session: AsyncSession,
    reseller_ids: list[int],
    body: FollowupBody,
    actor: str,
    day: dt.date,
) -> FollowupResult:
    snooze = await _resolve_snooze(session, body, day)
    now = dt.datetime.now(dt.UTC)
    # Segment at the moment of the touch, frozen into the log: the board recomputes segments
    # live, so without this "why did I contact them?" is unanswerable a month later.
    segments = {m.reseller_id: seg for m, seg, _ in await _board(session, day)}

    rows = (
        await session.execute(
            select(Reseller, Panel.key)
            .join(Panel, Reseller.panel_id == Panel.id)
            .where(Reseller.id.in_(reseller_ids))
        )
    ).all()
    if not rows:
        raise HTTPException(status_code=404, detail="no such reseller")

    for reseller, panel_key in rows:
        await crm.upsert_state(
            session,
            reseller.id,
            snoozed_until=snooze,
            muted=body.muted,
            note=body.pinned_note,
            now=now,
        )
        session.add(
            ResellerFollowup(
                reseller_id=reseller.id,
                reseller_admin_uuid=reseller.admin_uuid or "",
                reseller_name=reseller.name or "",
                panel_key=panel_key or "",
                segment=segments.get(reseller.id, ""),
                note=body.note or "",
                snoozed_until=snooze,
                muted=body.muted,
                actor=actor,
            )
        )
    await session.commit()
    return FollowupResult(updated=len(rows), snoozed_until=snooze, muted=body.muted)


@router.post("/reseller/{reseller_id}/followup", response_model=FollowupResult)
async def log_followup(
    reseller_id: int,
    body: FollowupBody,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(get_current_subject),
) -> FollowupResult:
    """Record "I followed this one up". Writes the history row and the state row; the reseller
    drops out of the default view until the snooze expires."""
    return await _log_followup(session, [reseller_id], body, actor, today())


@router.post("/followups/bulk", response_model=FollowupResult)
async def log_followups_bulk(
    body: BulkFollowupBody,
    session: AsyncSession = Depends(get_session),
    actor: str = Depends(get_current_subject),
) -> FollowupResult:
    """Same, for a whole selection — one pass down a segment after a batch of manual DMs."""
    return await _log_followup(session, body.reseller_ids, body, actor, today())


@router.delete("/reseller/{reseller_id}/snooze", response_model=FollowupResult)
async def clear_snooze(
    reseller_id: int, session: AsyncSession = Depends(get_session)
) -> FollowupResult:
    """Bring a snoozed or muted reseller straight back onto the queue. Does NOT log a
    follow-up and does not touch `touch_count` — undoing a snooze is not an outreach."""
    state = (
        await session.execute(
            select(ResellerCrmState).where(ResellerCrmState.reseller_id == reseller_id)
        )
    ).scalar_one_or_none()
    if state is None:
        # Never touched ⇒ already due. Idempotent rather than a 404.
        return FollowupResult(updated=0, snoozed_until=None, muted=False)
    state.snoozed_until = None
    state.muted = False
    await session.commit()
    return FollowupResult(updated=1, snoozed_until=None, muted=False)


@router.get("/followups", response_model=list[CrmFollowupRow])
async def followup_log(
    response: Response,
    reseller_id: int | None = None,
    q: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> list[CrmFollowupRow]:
    """The permanent outreach log, newest first. Append-only and never pruned — it keeps the
    denormalized reseller name, so history stays readable after a panel admin is deleted."""
    filters = []
    if reseller_id is not None:
        filters.append(ResellerFollowup.reseller_id == reseller_id)
    if q:
        filters.append(ResellerFollowup.reseller_name.ilike(f"%{q.strip()}%"))
    total = (
        await session.execute(
            select(func.count()).select_from(ResellerFollowup).where(*filters)
        )
    ).scalar_one()
    response.headers["X-Total-Count"] = str(total)
    rows = (
        await session.execute(
            select(ResellerFollowup)
            .where(*filters)
            .order_by(ResellerFollowup.created_at.desc(), ResellerFollowup.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars().all()
    return [_followup_row(f) for f in rows]
