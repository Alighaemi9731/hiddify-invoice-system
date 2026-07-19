"""Panels: CRUD + sync (owner-only)."""
from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.matching import normalize_host, normalize_path
from app.core import crypto
from app.core.db import SessionLocal, get_session
from app.core.security import get_current_subject
from app.models import EndUserSnapshot, Invoice, Panel, Reseller, SyncRun
from app.models.enums import InvoiceStatus, PanelStatus
from app.schemas.panel import (
    PanelCreate,
    PanelOut,
    PanelUpdate,
    SyncRunOut,
    SyncTestResult,
)
from app.services import payment_guard
from app.services import sync as sync_service
from app.services.panel_client import BackupJsonClient

log = logging.getLogger("api.panels")

_PRESENCE_SKEW = dt.timedelta(seconds=2)

router = APIRouter(
    prefix="/api/panels", tags=["panels"], dependencies=[Depends(get_current_subject)]
)


async def _to_out(session: AsyncSession, panel: Panel) -> PanelOut:
    # Count only non-owner resellers that are still present on the panel (seen in the
    # latest successful sync).  Resellers removed from Hiddify keep their DB row for
    # billing history but must not inflate the UI count.
    count_q = select(func.count(Reseller.id)).where(
        Reseller.panel_id == panel.id,
        Reseller.is_owner.is_(False),
    )
    if panel.status == PanelStatus.ok and panel.last_synced_at is not None:
        cutoff = panel.last_synced_at - _PRESENCE_SKEW
        count_q = count_q.where(
            or_(Reseller.last_seen_at.is_(None), Reseller.last_seen_at >= cutoff)
        )
    resellers_count = (await session.execute(count_q)).scalar_one()
    users_count = (
        await session.execute(
            select(func.count(EndUserSnapshot.id)).where(
                EndUserSnapshot.panel_id == panel.id
            )
        )
    ).scalar_one()
    return PanelOut(
        id=panel.id,
        key=panel.key,
        name=panel.name,
        host=panel.host,
        host_aliases=panel.host_alias_list,
        owner_uuid=panel.owner_uuid,
        enabled=panel.enabled,
        status=panel.status.value,
        source=panel.source.value,
        proxy_path_masked=crypto.mask(panel.proxy_path_enc),
        has_admin_api_key=bool(panel.admin_api_key_enc),
        last_synced_at=panel.last_synced_at,
        last_error=panel.last_error,
        backup_url=panel.backup_url,
        resellers_count=resellers_count,
        end_users_count=users_count,
    )


async def _get_or_404(session: AsyncSession, panel_id: int) -> Panel:
    panel = await session.get(Panel, panel_id)
    if panel is None:
        raise HTTPException(404, "Panel not found")
    return panel


@router.get("", response_model=list[PanelOut])
async def list_panels(session: AsyncSession = Depends(get_session)) -> list[PanelOut]:
    panels = (await session.execute(select(Panel).order_by(Panel.key))).scalars().all()
    return [await _to_out(session, p) for p in panels]


async def _same_physical_panel(
    session: AsyncSession, *, host: str | None, proxy_path: str | None,
    exclude_id: int | None = None
) -> Panel | None:
    """The existing row that points at the SAME physical Hiddify panel, if any.

    Identity is (host, secret proxy path) — two rows with both equal are the same box; there is no
    legitimate reason to register one panel twice. Registering it twice used to be silently accepted
    (only `key` was unique) and was quietly catastrophic: resellers are keyed by
    `(panel_id, admin_uuid)`, so the second row rebuilt every reseller and user from scratch — one
    real person then received TWO full invoices for the same traffic (the invoice unique constraint
    is per reseller_id, so both are "valid"), sales reports doubled, the per-panel sync lock no
    longer serialized the two rows against one box, enforcement could fight itself, and the bot's
    fail-closed link matching saw two candidates and refused to register the reseller at all — with
    an error blaming THEIR link. `proxy_path` is encrypted at rest, so this compares in Python over
    the panel rows (a handful) rather than in SQL.
    """
    want_host, want_path = normalize_host(host), normalize_path(proxy_path)
    if not want_host or not want_path:
        return None
    rows = (await session.execute(select(Panel))).scalars().all()
    for row in rows:
        if exclude_id is not None and row.id == exclude_id:
            continue
        try:
            if normalize_host(row.host) == want_host and normalize_path(row.proxy_path) == want_path:
                return row
        except Exception:  # noqa: BLE001 — an undecryptable legacy row must not block admin work
            continue
    return None


@router.post("", response_model=PanelOut, status_code=201)
async def create_panel(
    body: PanelCreate, background: BackgroundTasks, session: AsyncSession = Depends(get_session)
) -> PanelOut:
    # `body.key` is already normalized (stripped + lowercased) by the schema; compare
    # case-insensitively so a legacy uppercase row still counts as taken.
    exists = (
        await session.execute(select(Panel).where(func.lower(Panel.key) == body.key))
    ).scalar_one_or_none()
    if exists:
        raise HTTPException(409, f"پنلی با کلید «{body.key}» از قبل وجود دارد.")
    dup = await _same_physical_panel(session, host=body.host, proxy_path=body.proxy_path)
    if dup is not None:
        raise HTTPException(
            409,
            f"این پنل قبلاً با کلید «{dup.key}» ثبت شده است. ثبت دوبارهٔ یک پنل باعث می‌شود "
            "نماینده‌ها و کاربرها دو بار شمرده شوند و برای هر نماینده دو فاکتور صادر شود.",
        )
    panel = Panel(
        key=body.key,
        name=body.name or body.key,
        host=body.host.replace("https://", "").replace("http://", "").strip("/"),
        owner_uuid=body.owner_uuid,
        enabled=body.enabled,
    )
    panel.proxy_path = body.proxy_path  # encrypts
    if body.admin_api_key:
        panel.admin_api_key = body.admin_api_key
    if body.host_aliases:
        panel.set_host_aliases(body.host_aliases)
    session.add(panel)
    await session.commit()
    await session.refresh(panel)
    # Kick off the first sync automatically (status stays "unknown"=syncing until it
    # finishes), so a freshly added panel populates without a manual sync click.
    if panel.enabled:
        background.add_task(_sync_one_bg, panel.id)
    return await _to_out(session, panel)


@router.get("/{panel_id}", response_model=PanelOut)
async def get_panel(panel_id: int, session: AsyncSession = Depends(get_session)) -> PanelOut:
    return await _to_out(session, await _get_or_404(session, panel_id))


@router.patch("/{panel_id}", response_model=PanelOut)
async def update_panel(
    panel_id: int, body: PanelUpdate, background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> PanelOut:
    panel = await _get_or_404(session, panel_id)
    # Editing host/path can ALSO collide this row onto another row's physical panel — the same
    # double-counting/double-invoicing trap as creating a duplicate, just by the back door. Check
    # the post-edit identity before applying anything.
    if body.host is not None or body.proxy_path is not None:
        dup = await _same_physical_panel(
            session,
            host=body.host if body.host is not None else panel.host,
            proxy_path=body.proxy_path if body.proxy_path is not None else panel.proxy_path,
            exclude_id=panel.id,
        )
        if dup is not None:
            raise HTTPException(
                409,
                f"این آدرس متعلق به پنلی است که با کلید «{dup.key}» ثبت شده است. "
                "دو ردیف برای یک پنل باعث دوباره‌شماری نماینده‌ها و صدور فاکتور تکراری می‌شود.",
            )
    if body.name is not None:
        panel.name = body.name
    # Connection-relevant edits (host / proxy path / api key / owner uuid) change what the
    # panel returns, so re-sync after saving them.
    conn_changed = False
    if body.host is not None:
        panel.host = body.host.replace("https://", "").replace("http://", "").strip("/")
        conn_changed = True
    if body.owner_uuid is not None:
        panel.owner_uuid = body.owner_uuid
        conn_changed = True
    if body.proxy_path is not None:
        panel.proxy_path = body.proxy_path
        conn_changed = True
    if body.admin_api_key is not None:
        panel.admin_api_key = body.admin_api_key or None
        conn_changed = True
    # Aliases are matcher-only — they NEVER affect what the panel fetches, so changing them must
    # not trigger a re-sync (critical reliability rule). Set AFTER host so dedup-vs-host is correct.
    if body.host_aliases is not None:
        panel.set_host_aliases(body.host_aliases)
    if body.enabled is not None:
        panel.enabled = body.enabled
    if conn_changed and panel.enabled:
        panel.status = PanelStatus.unknown  # show "syncing…" until the re-sync finishes
    await session.commit()
    await session.refresh(panel)
    if conn_changed and panel.enabled:
        background.add_task(_sync_one_bg, panel.id)
    return await _to_out(session, panel)


@router.delete("/{panel_id}", status_code=204)
async def delete_panel(
    panel_id: int, force: bool = False, session: AsyncSession = Depends(get_session)
) -> None:
    """Delete a panel and everything below it (resellers, users, invoices, payments).

    Two independent guards, both of which answer 409 and are cleared by `force=true`:

      1. Cross-panel payment evidence (added in v1.93.0). A customer who owns resellers on several
         panels can settle all their invoices with ONE transfer, and that payment is owned by just
         one of those resellers. Deleting THIS panel would take the payment with it while another
         panel's invoice stays `paid` with nothing behind it — no txid, no receipt. The money facts
         for the survivor are archived to the FK-free ledger before the delete goes through.

      2. Blast radius (added in v1.94.1). Deleting a panel that has been billed permanently removes
         hundreds of invoice rows on a single click. The durable financial ledger
         (`financial_records`) survives — so this is not data LOSS in the accounting sense — but a
         destructive action of that size should state its scope and be confirmed, not slip past a
         generic «delete?» prompt. DRAFT invoices are throwaway (never sent, no money, discarded
         routinely) and never trigger the prompt, so a panel that was only ever calculated against
         still deletes freely.

    The deletion itself is left to SQLAlchemy's `all, delete-orphan` cascade on `Panel.resellers`
    (which unwinds resellers → invoices → lines in Python) plus the database FK cascades on
    snapshots/meters/payments/storefront bots — verified against PostgreSQL 16. It is NOT
    hand-rolled: the cascade already does exactly the right thing on the production engine.
    """
    panel = await _get_or_404(session, panel_id)
    reseller_ids = set((await session.execute(
        select(Reseller.id).where(Reseller.panel_id == panel_id)
    )).scalars().all())

    blocking = await payment_guard.blocking_payments(session, reseller_ids)
    real_invoices = (await session.execute(
        select(func.count(Invoice.id)).where(
            Invoice.panel_id == panel_id, Invoice.status != InvoiceStatus.draft)
    )).scalar_one()

    if not force and (blocking or real_invoices):
        raise HTTPException(
            409, await _delete_confirmation(session, panel, real_invoices, blocking))

    # Cross-panel case only: preserve the surviving invoices' money facts before the cascade runs.
    if blocking:
        await payment_guard.archive_before_delete(session, blocking)

    await session.delete(panel)
    await session.commit()


async def _delete_confirmation(
    session: AsyncSession, panel: Panel, real_invoices: int, blocking: list
) -> str:
    """State the blast radius so a large panel delete is a deliberate choice, not a slip."""
    parts: list[str] = []
    if real_invoices:
        breakdown: dict[InvoiceStatus, int] = {
            st: n for st, n in (await session.execute(
                select(Invoice.status, func.count(Invoice.id))
                .where(Invoice.panel_id == panel.id, Invoice.status != InvoiceStatus.draft)
                .group_by(Invoice.status)
            )).all()
        }
        paid = breakdown.get(InvoiceStatus.paid, 0)
        owed = sum(breakdown.get(s, 0) for s in
                   (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced))
        parts.append(
            f"حذفِ پنل «{panel.key}» {real_invoices} فاکتورِ واقعی را برای همیشه پاک می‌کند "
            f"({paid} پرداخت‌شده، {owed} پرداخت‌نشده). سوابقِ مالی در «تاریخچهٔ مالی» باقی می‌ماند، "
            "ولی خودِ فاکتورها و ریزِ کاربران حذف می‌شوند."
        )
    if blocking:
        foreign = sorted({i for b in blocking for i in b.foreign_invoice_ids})
        parts.append(
            f"همچنین {len(blocking)} پرداخت به فاکتورهای نماینده‌های پنل‌های دیگر هم مربوط است "
            f"(فاکتورهای {'، '.join(str(i) for i in foreign[:6])})؛ با حذف، سندِ آن پرداخت‌ها از "
            "بین می‌رود."
        )
    parts.append("اگر مطمئن هستید، «حذف اجباری» را بزنید.")
    return " ".join(parts)


async def _sync_one_bg(panel_id: int) -> None:
    """Background task: sync a single panel in its own DB session."""
    try:
        async with SessionLocal() as session:
            panel = await session.get(Panel, panel_id)
            if panel is not None:
                await sync_service.sync_panel(session, panel)
    except Exception:  # noqa: BLE001
        log.exception("background sync failed for panel %s", panel_id)


async def _sync_all_bg() -> None:
    try:
        async with SessionLocal() as session:
            await sync_service.sync_all(session)
    except Exception:  # noqa: BLE001
        log.exception("background sync-all failed")


@router.post("/{panel_id}/sync")
async def sync_panel(
    panel_id: int, background: BackgroundTasks, session: AsyncSession = Depends(get_session)
) -> dict:
    """Kick off a sync in the background and return immediately. The panel's
    `status` / `last_synced_at` update when it finishes (poll the panels list)."""
    panel = await _get_or_404(session, panel_id)
    panel.status = PanelStatus.unknown  # mark "syncing…" until it finishes
    await session.commit()
    background.add_task(_sync_one_bg, panel_id)
    return {"status": "started", "panel_id": panel_id}


@router.post("/sync-all")
async def sync_all(background: BackgroundTasks, session: AsyncSession = Depends(get_session)) -> dict:
    panels = (await session.execute(select(Panel).where(Panel.enabled.is_(True)))).scalars().all()
    background.add_task(_sync_all_bg)
    return {"status": "started", "panels": len(panels)}


@router.get("/{panel_id}/sync-runs", response_model=list[SyncRunOut])
async def sync_runs(
    panel_id: int, limit: int = 20, session: AsyncSession = Depends(get_session)
) -> list[SyncRunOut]:
    await _get_or_404(session, panel_id)
    runs = (
        await session.execute(
            select(SyncRun)
            .where(SyncRun.panel_id == panel_id)
            .order_by(SyncRun.started_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [SyncRunOut.model_validate(r, from_attributes=True) for r in runs]


@router.post("/{panel_id}/test", response_model=SyncTestResult)
async def test_connection(
    panel_id: int, session: AsyncSession = Depends(get_session)
) -> SyncTestResult:
    """Fetch the backup once without persisting — verifies credentials/connectivity."""
    panel = await _get_or_404(session, panel_id)
    try:
        data = await BackupJsonClient().fetch_backup(panel)
        return SyncTestResult(
            ok=True, admin_count=len(data.admins), user_count=len(data.users)
        )
    except Exception as exc:  # noqa: BLE001
        return SyncTestResult(ok=False, error=str(exc)[:500])
