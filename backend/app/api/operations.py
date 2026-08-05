"""On-demand operations the owner can trigger from the panel (mirrors scheduler jobs)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import signal

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal, get_session
from app.core.security import get_current_subject
from app.models import (
    BotUser,
    DeliveryLog,
    EndUserSnapshot,
    EnforcementAction,
    Invoice,
    InvoiceLine,
    Panel,
    Payment,
    Reseller,
    SyncRun,
    UsageMeter,
    UsageMeterEvent,
)
from app.models.enums import PanelStatus
from app.services import (
    backup as backup_service,
)
from app.services import (
    backup_delivery,
    channel_guard,
    delivery,
    dunning,
    enforcement,
    invoicing,
)
from app.services import (
    broadcast as broadcast_service,
)
from app.services import (
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
    text: str = ""
    audience: str = "all"          # one of broadcast_service.AUDIENCES (validated in the endpoints)
    panel_id: int | None = None    # optional single-panel restriction (combinable)
    # few_active: max active users • invoice_below/above: Toman • new_resellers: days (default 30).
    # REQUIRED (positive) for few_active/invoice_below/invoice_above — else the request is 400.
    threshold: float | None = None


# Audiences that are meaningless without a positive threshold. invoice_above with a
# missing threshold would otherwise match EVERY billable reseller (>= 0).
_THRESHOLD_REQUIRED = ("few_active", "invoice_below", "invoice_above")


def _validate_audience(audience: str, threshold: float | None = None) -> None:
    """An unknown audience must NEVER silently fall back to «all» (a typo would message
    everyone), and a threshold audience without a positive threshold is rejected."""
    if audience not in broadcast_service.AUDIENCES:
        raise HTTPException(400, "فیلترِ گیرندگان نامعتبر است.")
    if audience in _THRESHOLD_REQUIRED and (threshold is None or threshold <= 0):
        raise HTTPException(400, "این فیلتر به مقدارِ آستانهٔ معتبر نیاز دارد.")


class PanelMigrationBody(BaseModel):
    panel_id: int
    previous_host: str | None = None  # which old host to show as «قبلی» (defaults to the first alias)


@router.post("/dunning/run")
async def run_dunning(session: AsyncSession = Depends(get_session)) -> dict:
    return await dunning.run_dunning(session)


@router.post("/enforcement-queue/run")
async def run_enforcement_queue(session: AsyncSession = Depends(get_session)) -> dict:
    """Process a bounded slice of the queued live enforcement work."""
    return await enforcement.process_enforcement_queue(session)


@router.get("/storefront/below-cost-report")
async def below_cost_report(session: AsyncSession = Depends(get_session)) -> dict:
    """Which storefront plans are priced below the owning reseller's own cost, and what the sweep
    would do about them. Pure read — safe to call any time."""
    from app.services import storefront_belowcost

    return await storefront_belowcost.report(session)


@router.post("/storefront/below-cost-sweep")
async def below_cost_sweep(
    dry_run: bool = True, limit: int | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Disable below-cost storefront plans, close shops left with nothing to sell, and DM each
    owning reseller. Idempotent — safe to re-run.

    `dry_run` defaults to TRUE: an accidental POST reports instead of acting. Use `limit` to
    canary a few shops before the full run."""
    from app.services import storefront_belowcost

    return await storefront_belowcost.run_sweep(session, dry_run=dry_run, limit=limit)


@router.post("/storefront/reset-trial-quota")
async def reset_trial_quota(session: AsyncSession = Depends(get_session)) -> dict:
    """One-time cleanup: reset over-renewed free-trial configs back to 1 GB on their panels
    (the renewal-abuse fix). Idempotent — safe to re-run. Returns {checked, reset, skipped, failed}."""
    from app.services import storefront_subscription

    return await storefront_subscription.reset_over_renewed_trials(session)


@router.post("/refresh-rate")
async def refresh_rate(session: AsyncSession = Depends(get_session)) -> dict:
    """Fetch the live USDT→Toman rate now (Tetherland/Wallex) and cache it for billing/display.
    Also refreshes the TON→Toman rate, and the (derived) AVAX→Toman rate when AVAX is enabled."""
    from app.services import rates, settings_service

    usdt = await rates.refresh_auto_rate(session)
    ton = await rates.refresh_ton_rate(session)
    avax = None
    if await settings_service.get(session, "pay_avax_enabled", False):
        avax = await rates.refresh_avax_rate(session)
    if usdt is None and ton is None and avax is None:
        raise HTTPException(
            502, "دریافت نرخ آنلاین ناموفق بود؛ لطفاً بعداً دوباره تلاش کنید یا نرخ را به‌صورت دستی تنظیم کنید."
        )
    return {
        "rate": usdt, "ton": ton, "avax": avax,
        "effective": await rates.get_effective_rate(session),
        "ton_effective": await rates.get_ton_toman(session),
        "avax_effective": await rates.get_avax_toman(session),
    }


@router.get("/rates")
async def get_rates(session: AsyncSession = Depends(get_session)) -> dict:
    """Live exchange rates for display (read-only, NO network I/O — reads the cached values).
    `effective` is exactly what invoice generation will apply."""
    from app.services import rates, settings_service

    cfg = await settings_service.get_many(
        session,
        ["rate_mode", "rate_source", "toman_per_usdt", "toman_per_usdt_auto",
         "toman_per_usdt_auto_at", "ton_rate_mode", "ton_toman_auto", "ton_toman_manual",
         "rate_max_age_hours", "pay_ton_enabled",
         "avax_rate_mode", "avax_toman_auto", "avax_toman_manual", "pay_avax_enabled"],
    )

    def _int(v) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    mode = str(cfg.get("rate_mode") or "manual").lower()
    usdt_auto = _int(cfg.get("toman_per_usdt_auto"))
    max_age = _int(cfg.get("rate_max_age_hours")) or 48
    fresh = rates._rate_is_fresh(cfg.get("toman_per_usdt_auto_at"), max_age)  # noqa: SLF001
    return {
        "mode": mode,
        "source": str(cfg.get("rate_source") or "wallex").lower(),
        "effective": await rates.get_effective_rate(session),
        "usdt_auto": usdt_auto,
        "usdt_auto_at": cfg.get("toman_per_usdt_auto_at") or "",
        "usdt_manual": _int(cfg.get("toman_per_usdt")),
        "ton_mode": str(cfg.get("ton_rate_mode") or "auto").lower(),
        "ton_effective": await rates.get_ton_toman(session),
        "ton_auto": _int(cfg.get("ton_toman_auto")),
        "ton_manual": _int(cfg.get("ton_toman_manual")),
        "ton_enabled": bool(cfg.get("pay_ton_enabled")),
        "avax_mode": str(cfg.get("avax_rate_mode") or "auto").lower(),
        "avax_effective": await rates.get_avax_toman(session),
        "avax_auto": _int(cfg.get("avax_toman_auto")),
        "avax_manual": _int(cfg.get("avax_toman_manual")),
        "avax_enabled": bool(cfg.get("pay_avax_enabled")),
        "stale": bool(mode == "auto" and usdt_auto > 0 and not fresh),
    }


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
    # Order matters for FK constraints: children before parents. UsageMeter and BotUser
    # have NO foreign keys, so stale rows would otherwise survive a wipe and (for meters)
    # be re-billed against a freshly-seeded panel. FinancialRecord is intentionally kept
    # (durable ledger); app_users + settings are kept (owner login + config).
    #
    # ⚠️ LEDGER SAFETY — keep this a per-row DELETE, never `TRUNCATE ... RESTART IDENTITY`.
    # financial_records.invoice_id is UNIQUE and mirrors the invoices id. DELETE preserves the
    # invoices id sequence on Postgres, so new invoices get fresh ids that never collide with the
    # kept ledger rows. TRUNCATE ... RESTART IDENTITY would reset the sequence, and the next
    # invoice would reuse an old id and silently overwrite a historic financial_records row.
    for model in (
        InvoiceLine, Payment, DeliveryLog, EnforcementAction, Invoice,
        UsageMeter, UsageMeterEvent, EndUserSnapshot, SyncRun, BotUser, Reseller, Panel,
    ):
        await session.execute(delete(model))
    await session.commit()
    return {"status": "ok", "message": "همهٔ داده‌ها پاک شد (به‌جز حساب مدیر، تنظیمات و تاریخچهٔ مالی)."}


@router.post("/set-domain")
async def set_domain(body: DomainBody, session: AsyncSession = Depends(get_session)) -> dict:
    """Set the panel's public domain and trigger automatic HTTPS via Caddy."""
    from app.services import domain_setup

    return await domain_setup.set_domain(session, body.domain, body.acme_email)


async def _broadcast_bg(text: str, reachable: list, unregistered: int) -> None:
    """Background task: send the broadcast in its own DB session (the request's session/bot are
    already closed by the time this runs)."""
    try:
        async with SessionLocal() as session:
            await broadcast_service.run_broadcast(
                session, text, reachable, unregistered=unregistered)
    except Exception:  # noqa: BLE001
        log.exception("background broadcast failed")


@router.post("/broadcast")
async def broadcast(
    body: BroadcastBody, background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Resolve recipients fast, launch the send in the BACKGROUND, and return immediately. The
    result summary is delivered to the owner's Telegram when it finishes (and is also visible via
    the live status endpoint). Avoids the request timeout on large audiences."""
    _validate_audience(body.audience, body.threshold)
    reachable, unregistered = await broadcast_service.resolve_recipients(
        session, body.audience, body.panel_id, body.threshold)
    if body.text.strip() and reachable:
        background.add_task(_broadcast_bg, body.text, reachable, len(unregistered))
    return {"status": "started", "total": len(reachable), "unregistered": len(unregistered)}


@router.get("/broadcast/status")
async def broadcast_status() -> dict:
    """Live in-memory snapshot of the current/last broadcast run (no DB) for the panel's progress."""
    return broadcast_service.current_status()


@router.post("/broadcast/preview")
async def broadcast_preview(
    body: BroadcastBody, session: AsyncSession = Depends(get_session)
) -> broadcast_service.BroadcastResult:
    """Resolve who WOULD receive the broadcast (no message sent), so the owner can verify the
    filter and see the exact recipient list before sending."""
    _validate_audience(body.audience, body.threshold)
    return await broadcast_service.preview(
        session, audience=body.audience, panel_id=body.panel_id, threshold=body.threshold
    )


# ----- panel-migration broadcast (personalized «new panel address» per reseller) -----
_PRESENCE_SKEW = dt.timedelta(seconds=2)


def _present(reseller: Reseller, panel: Panel) -> bool:
    if panel.status != PanelStatus.ok or panel.last_synced_at is None:
        return True
    if reseller.last_seen_at is None:
        return True
    return reseller.last_seen_at >= panel.last_synced_at - _PRESENCE_SKEW


async def _panel_migration_targets(session: AsyncSession, panel: Panel) -> tuple[list[Reseller], int]:
    """Present non-owner resellers on the panel, split into (registered-in-bot, unregistered-count).
    Each registered reseller gets their own link (NOT deduped by chat_id)."""
    rows = (await session.execute(
        select(Reseller).where(Reseller.panel_id == panel.id, Reseller.is_owner.is_(False))
    )).scalars().all()
    present = [r for r in rows if _present(r, panel)]
    registered = [r for r in present if r.bot_chat_id]
    return registered, len(present) - len(registered)


def _resolve_prev_host(panel: Panel, requested: str | None) -> str:
    aliases = panel.host_alias_list
    return (requested or "").strip() or (aliases[0] if aliases else "")


async def _panel_migration_bg(reachable: list, texts: dict, unregistered: int) -> None:
    try:
        async with SessionLocal() as session:
            await broadcast_service.run_broadcast(
                session, "", reachable, unregistered=unregistered,
                render=lambda rec: texts.get(rec["reseller_id"], ""), parse_mode="HTML")
    except Exception:  # noqa: BLE001
        log.exception("background panel-migration broadcast failed")


@router.post("/broadcast/panel-migration")
async def broadcast_panel_migration(
    body: PanelMigrationBody, background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Send each registered reseller on the panel a personalized «new address» message (their own
    new + previous link), in the background via the shared broadcast worker."""
    panel = await session.get(Panel, body.panel_id)
    if panel is None:
        raise HTTPException(404, "Panel not found")
    prev = _resolve_prev_host(panel, body.previous_host)
    if not prev:
        raise HTTPException(
            400, "هاستِ قبلی ثبت نشده است؛ ابتدا در ویرایشِ پنل یک «هاستِ قبلی/مستعار» اضافه کنید.")
    registered, unregistered = await _panel_migration_targets(session, panel)
    texts = {r.id: broadcast_service.migration_text(panel, prev, r.admin_uuid, r.link_tag)
             for r in registered}
    reachable = [
        broadcast_service.Recipient(
            reseller_id=r.id, name=r.name or "—", panel=panel.key,
            chat_id=r.bot_chat_id, status="pending")
        for r in registered
    ]
    if reachable:
        background.add_task(_panel_migration_bg, reachable, texts, unregistered)
    return {"status": "started", "total": len(reachable),
            "unregistered": unregistered, "previous_host": prev}


@router.post("/broadcast/panel-migration/preview")
async def broadcast_panel_migration_preview(
    body: PanelMigrationBody, session: AsyncSession = Depends(get_session)
) -> dict:
    """Recipient counts + a sample of the two links, so the owner can verify before sending."""
    panel = await session.get(Panel, body.panel_id)
    if panel is None:
        raise HTTPException(404, "Panel not found")
    prev = _resolve_prev_host(panel, body.previous_host)
    registered, unregistered = await _panel_migration_targets(session, panel)
    sample = registered[0] if registered else None
    sample_uuid = sample.admin_uuid if sample else "00000000-0000-0000-0000-000000000000"
    sample_tag = sample.link_tag if sample else None
    return {
        "total": len(registered), "unregistered": unregistered,
        "new_host": panel.host, "previous_host": prev, "aliases": panel.host_alias_list,
        "sample_new_link": panel.admin_link(sample_uuid, tag=sample_tag),
        "sample_previous_link": panel.admin_link(sample_uuid, tag=sample_tag, host=prev) if prev else "",
    }


@router.post("/channel-guard")
async def channel_guard_run(session: AsyncSession = Depends(get_session)) -> dict:
    """Run the channel guard now (dry-run unless channel_kick_enabled is on)."""
    return await channel_guard.enforce_channel(session)


# ------------------------------- backup / restore -------------------------------
@router.get("/backup/download")
async def backup_download(session: AsyncSession = Depends(get_session)) -> StreamingResponse:
    # Built on disk and streamed from there: a browser download must not cost the API process
    # the whole archive in RAM (same reason the scheduler's auto-backup no longer does).
    built, name = await backup_service.create_backup_file(session)
    # A manual panel download is a real backup the owner now holds → count it as the last backup.
    await backup_service.mark_backup_done(session)
    size = built.stat().st_size

    def _stream():  # noqa: ANN202
        try:
            with built.open("rb") as fh:
                while chunk := fh.read(1 << 20):
                    yield chunk
        finally:
            backup_service.discard_temp_archive(built)

    return StreamingResponse(
        _stream(), media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Content-Length": str(size),
        },
    )


@router.post("/backup/send")
async def backup_send(session: AsyncSession = Depends(get_session)) -> dict:
    """Build a backup now and send it to the owner's Telegram PV."""
    return await backup_delivery.send_backup_to_owner(session)


@router.post("/backup/restore")
async def backup_restore(
    file: UploadFile,
    passphrase: str | None = Form(None),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Restore the system from an uploaded backup .zip. On success the backend
    reconnects and restarts itself automatically — no server terminal needed.

    `passphrase` is required only for an encrypted backup; if omitted, the configured
    `backup_passphrase` setting is used as a fallback."""
    if not (file.filename or "").endswith(".zip"):
        raise HTTPException(400, "فایل باید زیپ (.zip) باشد")
    if file.size and file.size > backup_service.MAX_ARCHIVE_BYTES:
        raise HTTPException(400, "حجم فایل پشتیبان بیش از حد مجاز است.")
    if not (passphrase or "").strip():
        from app.services import settings_service

        passphrase = await settings_service.get(session, "backup_passphrase", "") or None
    try:
        content = await file.read()
        if len(content) > backup_service.MAX_ARCHIVE_BYTES:
            raise HTTPException(400, "حجم فایل پشتیبان بیش از حد مجاز است.")
        # The dump/restore subprocess work is blocking — keep it off the event loop.
        result = await asyncio.to_thread(
            backup_service.restore_from_zip, content, passphrase=passphrase
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"بازیابی ناموفق بود: {exc}") from exc

    if result.get("restored"):
        # Drop pooled connections (they point at the pre-restore DB) and restart so
        # everything comes up cleanly against the restored data.
        from app.core.db import engine

        await engine.dispose()
        result["note"] = "بازیابی انجام شد. سرویس به‌صورت خودکار مجدداً راه‌اندازی می‌شود (لطفاً چند ثانیه صبر کنید)."
        _schedule_self_restart(1.5)
    return result


@router.post("/restart")
async def restart_service() -> dict:
    """Restart the backend service from the panel (no server terminal needed)."""
    _schedule_self_restart(1.0)
    return {"status": "restarting", "message": "سرویس در حال راه‌اندازی مجدد است (لطفاً چند ثانیه صبر کنید)."}


# ------------------------------- self-update -------------------------------
# The backend container is intentionally sandboxed (no Docker socket), so it can't
# rebuild itself. Instead the panel writes a request flag into the shared data volume
# and a tiny host-side watcher (systemd unit `hiddify-updater`, installed by
# deploy/install.sh) downloads and verifies an exact GitHub release asset before
# rebuilding. The watcher reports progress back into the same volume. These files live under
# /app/data (the `app_data` volume), which the host mounts at the repo's data dir.
_UPDATE_DIR = os.environ.get("UPDATE_DIR", "/app/data")
_UPDATE_REQUEST = os.path.join(_UPDATE_DIR, ".update-requested")
_UPDATE_STATUS = os.path.join(_UPDATE_DIR, ".update-status")
# Persistent marker the host watcher writes on startup (survives request/status resets),
# so the panel reliably knows the watcher is installed even between updates.
_UPDATE_ALIVE = os.path.join(_UPDATE_DIR, ".updater-alive")


def _read_update_status() -> dict:
    """Read the watcher's status file: {phase, version, message, ts}. phase is one of
    idle | requested | running | done | failed. Defaults to idle when absent."""
    import json

    try:
        with open(_UPDATE_STATUS, encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001 — a half-written file: treat as running
        return {"phase": "running"}
    # No status file but a request is pending → the watcher hasn't picked it up yet.
    if os.path.exists(_UPDATE_REQUEST):
        return {"phase": "requested"}
    return {"phase": "idle"}


@router.post("/update")
async def update_now() -> dict:
    """Ask the host-side watcher to update the system to the latest release.

    Writes a request flag the watcher polls; returns immediately. Poll
    GET /api/ops/update-status to follow progress; the site briefly goes down while the
    containers rebuild, then comes back on the new version."""
    from app import __version__

    try:
        os.makedirs(_UPDATE_DIR, exist_ok=True)
        # Reset any stale status, then drop the request flag.
        try:
            os.remove(_UPDATE_STATUS)
        except FileNotFoundError:
            pass
        with open(_UPDATE_REQUEST, "w", encoding="utf-8") as fh:
            fh.write(__version__)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"ثبت درخواست به‌روزرسانی ناموفق بود: {exc}") from exc
    return {
        "status": "requested",
        "current_version": __version__,
        "message": (
            "درخواست به‌روزرسانی ثبت شد. اگر سرویس به‌روزرسانیِ خودکار روی سرور نصب باشد، "
            "چند دقیقه بعد سامانه به آخرین نسخه به‌روزرسانی می‌شود و دوباره در دسترس قرار می‌گیرد."
        ),
    }


@router.get("/update-status")
async def update_status() -> dict:
    """Current update phase for the panel to poll. updater_installed=False means the
    host watcher isn't present (older installs) — the request would sit unhandled."""
    from app import __version__

    st = _read_update_status()
    st.setdefault("current_version", __version__)
    # The watcher is installed if its persistent alive-marker exists (written on startup),
    # OR it has touched the status file / is mid-run. The alive-marker is the reliable
    # signal because it survives the status reset that happens on each update request.
    st["updater_installed"] = (
        os.path.exists(_UPDATE_ALIVE)
        or os.path.exists(_UPDATE_STATUS)
        or st.get("phase") in ("running", "done", "failed")
    )
    return st
