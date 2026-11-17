"""A separate registration restriction; paying debt never clears this reason."""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EnforcementAction, Invoice, Panel, Reseller
from app.models.enums import (
    EnforcementActionType,
    EnforcementState,
    InvoiceStatus,
    PanelStatus,
)
from app.services import invoice_state, issuance, periods, settings_service
from app.services.invoice_engine import select_billable_roots
from app.services.job_lock import session_lock


def aware(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value


async def blocking(
    session: AsyncSession, reseller: Reseller, *, now: dt.datetime | None = None,
) -> bool:
    if reseller.bot_chat_id is not None or reseller.is_owner or reseller.exclude_from_billing:
        return False
    if reseller.registration_deadline_at is None:
        return False
    if not await settings_service.get(session, "registration_enforcement_enabled", True):
        return False
    return aware(reseller.registration_deadline_at) <= aware(now or dt.datetime.now(dt.timezone.utc))


async def financial_blocking(session: AsyncSession, reseller: Reseller) -> bool:
    cfg = await issuance.schedule_days(session)
    today = periods.today()
    invoices = (await session.scalars(select(Invoice).where(
        Invoice.reseller_id == reseller.id, Invoice.status.in_(invoice_state.OWED),
    ))).all()
    return any((not inv.deferred_until or inv.deferred_until <= today) and (
        inv.status == InvoiceStatus.enforced or (
            (anchor := issuance.collection_anchor(inv)) is not None
            and (today - anchor).days >= cfg["enforcement_day"]
        )) for inv in invoices)


async def only_registration_suspension(session: AsyncSession, reseller: Reseller) -> bool:
    """Keep the cause across completion and the queued restore; don't invent overdue debt."""
    sources = (await session.scalars(select(EnforcementAction).where(
        EnforcementAction.reseller_id == reseller.id,
        EnforcementAction.action == EnforcementActionType.disable_users,
        EnforcementAction.dry_run.is_(False),
    ).order_by(EnforcementAction.id.desc()).limit(20))).all()
    primary = next((a for a in sources if not (a.snapshot or {}).get("reassert")), None)
    registration_source = primary and (primary.snapshot or {}).get("restriction_reason") == "registration"
    return bool((reseller.registration_blocked or registration_source)
                and not await financial_blocking(session, reseller))


async def completed(session: AsyncSession, reseller: Reseller) -> None:
    """Also called after a link is registered; only release OUR own suspension."""
    from app.services import enforcement
    was_blocked = reseller.registration_blocked
    reseller.registration_blocked = False
    reseller.registration_deadline_at = None
    await session.commit()
    if not was_blocked or await financial_blocking(session, reseller):
        return
    source = (await session.scalars(select(EnforcementAction).where(
        EnforcementAction.reseller_id == reseller.id,
        EnforcementAction.action == EnforcementActionType.disable_users,
        EnforcementAction.dry_run.is_(False),
    ).order_by(EnforcementAction.id.desc()).execution_options(populate_existing=True))).all()
    source = [a for a in source if not (a.snapshot or {}).get("reassert")]
    if source and (source[0].snapshot or {}).get("restriction_reason") == "registration":
        await enforcement.queue_restore(session, reseller, reason="registration-completed")


async def run(session: AsyncSession, *, now: dt.datetime | None = None) -> dict:
    from app.services import enforcement
    now = aware(now or dt.datetime.now(dt.timezone.utc))
    async with session_lock(session, 734_137_053) as acquired:
        if not acquired:
            return {"queued": 0}
        hours = int(await settings_service.get(session, "registration_grace_hours", 72))
        enabled = bool(await settings_service.get(session, "registration_enforcement_enabled", True))
        panels = {p.id: p for p in (await session.scalars(select(Panel))).all()}
        groups: dict[int, list[Reseller]] = defaultdict(list)
        for r in (await session.scalars(select(Reseller))).all():
            groups[r.panel_id].append(r)
        roots = {r.id for rows in groups.values() for r in select_billable_roots(rows)}
        queued = 0
        for rows in groups.values():
            for r in rows:
                p = panels.get(r.panel_id)
                eligible = r.id in roots and p and p.enabled
                if (r.bot_chat_id is not None and (r.registration_blocked or r.registration_deadline_at)) or (r.registration_blocked and (not eligible or not enabled)):
                    await completed(session, r)
                    continue
                if r.bot_chat_id is not None or not eligible or not enabled or p is None or p.status != PanelStatus.ok or p.maintenance:
                    continue
                from app.services.presence import reseller_absent
                if p.last_synced_at is None or reseller_absent(r, p):
                    continue
                if r.registration_deadline_at is None:
                    r.registration_deadline_at = now + dt.timedelta(hours=hours)
                    await session.commit()
                if not await blocking(session, r, now=now):
                    continue
                r.registration_blocked = True
                await session.commit()
                if r.enforcement_state != EnforcementState.enforced:
                    action = await enforcement.queue_enforcement(session, r, reason="registration")
                    queued += int(not action.dry_run)
        if enabled and await settings_service.get(session, "enforcement_enabled", False):
            await enforcement.reassert_enforced(session, registration_only=True)
        return {"queued": queued}
