"""On-demand operations the owner can trigger from the panel (mirrors scheduler jobs)."""
from __future__ import annotations

import asyncio
import io
import logging
import os
import signal

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import get_current_subject
from app.models import (
    DeliveryLog, EndUserSnapshot, EnforcementAction, Invoice, InvoiceLine,
    Panel, Payment, Reseller, SyncRun,
)
from app.services import (
    backup as backup_service,
    backup_delivery,
    broadcast as broadcast_service,
    channel_guard,
    delivery,
    dunning,
    invoicing,
    sync as sync_service,
)
from app.services.periods import parse_period, previous_month

log = logging.getLogger("operations")

router = APIRouter(
    prefix="/api/ops", tags=["operations"], dependencies=[Depends(get_current_subject)]
)


def _schedule_self_restart(delay: float = 1.0) -> None:
    """Gracefully exit the process after `delay`s. Docker's `restart: unless-stopped`
    brings the backend straight back up — so the owner never needs the server terminal."""
    def _stop() -> None:
        log.info("self-restart: sending SIGTERM to pid %s", os.getpid())
        os.kill(os.getpid(), signal.SIGTERM)

    try:
        asyncio.get_event_loop().call_later(delay, _stop)
    except Exception:  # noqa: BLE001
        log.warning("self-restart scheduling failed", exc_info=True)


class WipeBody(BaseModel):
    confirm: str  # must equal "DELETE" to proceed


class DomainBody(BaseModel):
    domain: str
    acme_email: str | None = None


class BroadcastBody(BaseModel):
    text: str
    audience: str = "all"          # all | debtors | panel
    panel_id: int | None = None


@router.post("/dunning/run")
async def run_dunning(session: AsyncSession = Depends(get_session)) -> dict:
    return await dunning.run_dunning(session)


@router.post("/run-monthly")
async def run_monthly(
    period: str | None = None,
    send: bool = True,
    sync_first: bool = True,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Full monthly cycle: (sync) → generate invoices → (send). Defaults to previous month."""
    p = parse_period(period) if period else previous_month()
    result: dict = {"period": p.label}
    if sync_first:
        runs = await sync_service.sync_all(session)
        result["synced_panels"] = len(runs)
    summary = await invoicing.generate_invoices(session, p)
    result["generated"] = summary.__dict__
    if send:
        result["delivery"] = await delivery.send_period(session, p.label)
    return result


@router.post("/wipe-data")
async def wipe_data(body: WipeBody, session: AsyncSession = Depends(get_session)) -> dict:
    """Delete ALL business data (panels, resellers, invoices, payments, snapshots,
    logs). Keeps the owner login and settings. Irreversible — guard with confirm."""
    if body.confirm != "DELETE":
        raise HTTPException(400, "برای تأیید باید عبارت DELETE ارسال شود")
    # Order matters for FK constraints: children before parents.
    for model in (
        InvoiceLine, Payment, DeliveryLog, EnforcementAction, Invoice,
        EndUserSnapshot, SyncRun, Reseller, Panel,
    ):
        await session.execute(delete(model))
    await session.commit()
    return {"status": "ok", "message": "همهٔ داده‌ها پاک شد (به‌جز حساب مدیر و تنظیمات)."}


@router.post("/set-domain")
async def set_domain(body: DomainBody, session: AsyncSession = Depends(get_session)) -> dict:
    """Set the panel's public domain and trigger automatic HTTPS via Caddy."""
    from app.services import domain_setup

    return await domain_setup.set_domain(session, body.domain, body.acme_email)


@router.post("/broadcast")
async def broadcast(body: BroadcastBody, session: AsyncSession = Depends(get_session)) -> dict:
    return await broadcast_service.broadcast(
        session, body.text, audience=body.audience, panel_id=body.panel_id
    )


@router.post("/channel-guard")
async def channel_guard_run(session: AsyncSession = Depends(get_session)) -> dict:
    """Run the channel guard now (dry-run unless channel_kick_enabled is on)."""
    return await channel_guard.enforce_channel(session)


# ------------------------------- backup / restore -------------------------------
@router.get("/backup/download")
async def backup_download(session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    data, name = await backup_service.create_backup(session)
    return StreamingResponse(
        io.BytesIO(data), media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/backup/send")
async def backup_send(session: AsyncSession = Depends(get_session)) -> dict:
    """Build a backup now and send it to the owner's Telegram PV."""
    return await backup_delivery.send_backup_to_owner(session)


@router.post("/backup/restore")
async def backup_restore(file: UploadFile, session: AsyncSession = Depends(get_session)) -> dict:
    """Restore the system from an uploaded backup .zip. On success the backend
    reconnects and restarts itself automatically — no server terminal needed."""
    if not (file.filename or "").endswith(".zip"):
        raise HTTPException(400, "فایل باید زیپ (.zip) باشد")
    try:
        content = await file.read()
        result = backup_service.restore_from_zip(content)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"بازیابی ناموفق بود: {exc}")

    if result.get("restored"):
        # Drop pooled connections (they point at the pre-restore DB) and restart so
        # everything comes up cleanly against the restored data.
        from app.core.db import engine

        await engine.dispose()
        result["note"] = "بازیابی انجام شد. سرویس به‌صورت خودکار ری‌استارت می‌شود (چند ثانیه صبر کنید)."
        _schedule_self_restart(1.5)
    return result


@router.post("/restart")
async def restart_service() -> dict:
    """Restart the backend service from the panel (no server terminal needed)."""
    _schedule_self_restart(1.0)
    return {"status": "restarting", "message": "سرویس در حال راه‌اندازی مجدد است (چند ثانیه صبر کنید)."}
