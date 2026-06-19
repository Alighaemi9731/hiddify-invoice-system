"""Reseller web-portal API (prefix /api/portal) — the standalone site each reseller logs into via a
Telegram one-time link. Every route except the public token exchange depends on
`get_current_reseller` and is scoped to the caller's own reseller rows; client-supplied ids are never
trusted. Mirrors the data the Telegram bot already shows, reusing the same services."""
from __future__ import annotations

import datetime as dt
import html as _html
import os

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import invoice_code, payment_code
from app.core.db import get_session
from app.core.portal_auth import (
    ResellerContext,
    create_portal_session_token,
    get_current_reseller,
    verify_portal_login_token,
)
from app.models import Invoice, Panel, Payment, Reseller
from app.models.enums import InvoiceStatus
from app.services import invoice_pdf as invoice_pdf_service
from app.services import owner_notify, payment_methods, payments, reseller_report
from app.services.periods import current_month, parse_period
from app.services.periods import today as tehran_today

# Receipt upload safety cap (matches a phone screenshot; refuse anything larger).
_MAX_PROOF_BYTES = 8 * 1024 * 1024

router = APIRouter(prefix="/api/portal", tags=["portal"])

# Delivered-but-unpaid states (the reseller's "due") — mirrors the bot.
_OWED = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)


class ExchangeBody(BaseModel):
    token: str


async def _panel_keys(session: AsyncSession, panel_ids: set[int]) -> dict[int, Panel]:
    if not panel_ids:
        return {}
    rows = (await session.execute(select(Panel).where(Panel.id.in_(panel_ids)))).scalars().all()
    return {p.id: p for p in rows}


# ------------------------------ auth ------------------------------
@router.post("/auth/exchange")
async def exchange(body: ExchangeBody, session: AsyncSession = Depends(get_session)) -> dict:
    """Exchange a bot-issued one-time login token for a reseller session token. Public (the token
    itself is the credential). Only succeeds if the Telegram id still has reseller rows."""
    chat_id = verify_portal_login_token(body.token)
    if chat_id is None:
        raise HTTPException(401, "لینکِ ورود نامعتبر یا منقضی شده است؛ از ربات دوباره وارد شوید.")
    has = (await session.execute(
        select(Reseller.id).where(Reseller.bot_chat_id == chat_id).limit(1)
    )).first()
    if has is None:
        raise HTTPException(403, "این حساب نماینده‌ای در سامانه ندارد.")
    return {"access_token": create_portal_session_token(chat_id), "token_type": "bearer"}


@router.get("/me")
async def me(
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict:
    panels = await _panel_keys(session, {r.panel_id for r in ctx.resellers})
    return {
        "chat_id": ctx.chat_id,
        "resellers": [
            {
                "id": r.id, "name": r.name, "admin_uuid": r.admin_uuid,
                "panel_key": panels[r.panel_id].key if r.panel_id in panels else "?",
                "link_tag": r.link_tag, "enforcement_state": r.enforcement_state.value,
            }
            for r in ctx.resellers
        ],
    }


# ------------------------------ dashboard ------------------------------
@router.get("/summary")
async def summary(
    period: str | None = None,
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Month-to-date estimate (own + subs) + outstanding debt + a daily sale trend, aggregated over
    ALL the caller's reseller rows. Mirrors the bot's «interim» and the invoice engine."""
    try:
        p = parse_period(period) if period else current_month()
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "دورهٔ نامعتبر است.") from exc
    n_days = p.end.day
    buckets = [0.0] * n_days
    est_amount = 0
    est_gb = 0.0
    est_users = 0
    per_reseller = []
    panels = await _panel_keys(session, {r.panel_id for r in ctx.resellers})
    for r in ctx.resellers:
        bundle = await reseller_report.node_invoice(session, r, p)
        amount = int(bundle.base_amount_toman) if bundle else 0
        gb = float(bundle.total_gb) if bundle else 0.0
        users = int(bundle.users_count) if bundle else 0
        est_amount += amount
        est_gb += gb
        est_users += users
        per_reseller.append({
            "id": r.id, "name": r.name,
            "panel_key": panels[r.panel_id].key if r.panel_id in panels else "?",
            "amount_toman": amount, "gb": round(gb, 2), "users": users,
        })
        if bundle:
            for line in bundle.lines:
                if line.start_date and p.contains(line.start_date):
                    buckets[line.start_date.day - 1] += line.usage_gb * bundle.price_per_gb

    owed = (await session.execute(
        select(Invoice).where(Invoice.reseller_id.in_(ctx.ids), Invoice.status.in_(_OWED))
    )).scalars().all()
    today = tehran_today()
    due = [i for i in owed if not (i.deferred_until and i.deferred_until > today)]
    outstanding = sum(float(i.amount_toman) for i in due)

    return {
        "period": p.label,
        "estimate": {"amount_toman": est_amount, "gb": round(est_gb, 2), "users": est_users},
        "per_reseller": per_reseller,
        "outstanding": {"amount_toman": round(outstanding), "count": len(due)},
        "reseller_count": len(ctx.resellers),
        "trend": [
            {"day": i + 1,
             "date": dt.date(p.start.year, p.start.month, i + 1).isoformat(),
             "amount_toman": round(v)}
            for i, v in enumerate(buckets)
        ],
    }


# ------------------------------ invoices ------------------------------
@router.get("/invoices")
async def invoices(
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """The caller's issued invoices (newest first) — owed ones first for paying."""
    panels = await _panel_keys(session, {r.panel_id for r in ctx.resellers})
    rows = (await session.execute(
        select(Invoice)
        .where(Invoice.reseller_id.in_(ctx.ids),
               Invoice.status != InvoiceStatus.draft)
        .order_by(Invoice.period_start.desc(), Invoice.id.desc())
        .limit(60)
    )).scalars().all()
    today = tehran_today()
    return [
        {
            "id": i.id, "number": invoice_code(i.id), "period_label": i.period_label,
            "panel_key": panels[i.panel_id].key if i.panel_id in panels else "?",
            "usage_gb": float(i.usage_gb), "amount_toman": float(i.amount_toman),
            "status": i.status.value,
            "owed": i.status in _OWED and not (i.deferred_until and i.deferred_until > today),
            "deferred_until": i.deferred_until.isoformat() if i.deferred_until else None,
            "created_at": i.created_at.isoformat() if i.created_at else None,
        }
        for i in rows
    ]


@router.get("/invoices/{invoice_id}/pdf")
async def invoice_pdf(
    invoice_id: int,
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    inv = await session.get(Invoice, invoice_id)
    if inv is None or inv.reseller_id not in ctx.ids:  # ownership: never trust the URL id
        raise HTTPException(404, "Invoice not found")
    path, filename = await invoice_pdf_service.render_invoice_pdf(session, inv)
    return FileResponse(path, media_type="application/pdf", filename=filename)


# ------------------------------ payments ------------------------------
@router.get("/payments")
async def list_payments(
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    rows = (await session.execute(
        select(Payment, Invoice.period_label)
        .outerjoin(Invoice, Payment.invoice_id == Invoice.id)
        .where(Payment.reseller_id.in_(ctx.ids))
        .order_by(Payment.created_at.desc())
        .limit(60)
    )).all()
    return [
        {
            "id": pmt.id,
            "number": payment_code(pmt.id), "method": pmt.method.value, "status": pmt.status.value,
            "chain": pmt.chain, "txid": pmt.txid,
            "amount_toman": float(pmt.amount_toman or 0),
            "invoice_period": period,
            "has_proof": bool(pmt.proof_path),
            "created_at": pmt.created_at.isoformat() if pmt.created_at else None,
            "verified_at": pmt.verified_at.isoformat() if pmt.verified_at else None,
        }
        for pmt, period in rows
    ]


@router.get("/payments/{payment_id}/proof")
async def payment_proof(
    payment_id: int,
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """Serve the caller's OWN deposit screenshot (scoped to their resellers). Same path-traversal
    guard as the owner endpoint so a tampered proof_path can't disclose an arbitrary file."""
    p = await session.get(Payment, payment_id)
    if p is None or p.reseller_id not in ctx.ids:  # ownership: never trust the URL id
        raise HTTPException(404, "Payment not found")
    if not p.proof_path or not os.path.exists(p.proof_path):
        raise HTTPException(404, "No proof image for this payment")
    proof_dir = os.path.realpath("data/payment_proofs")
    if not os.path.realpath(p.proof_path).startswith(proof_dir + os.sep):
        raise HTTPException(404, "No proof image for this payment")
    return FileResponse(p.proof_path, media_type="image/jpeg", filename=f"proof_{payment_id}.jpg")


# ------------------------------ panels ------------------------------
@router.get("/panels")
async def panels(
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """The caller's own admin link on each panel (current host; + previous host if migrated)."""
    pmap = await _panel_keys(session, {r.panel_id for r in ctx.resellers})
    out = []
    for r in ctx.resellers:
        panel = pmap.get(r.panel_id)
        if panel is None:
            continue
        aliases = panel.host_alias_list
        out.append({
            "reseller_id": r.id, "name": r.name,
            "panel_key": panel.key, "panel_name": panel.name or panel.key,
            "link": panel.admin_link(r.admin_uuid, tag=r.link_tag),
            "previous_link": (
                panel.admin_link(r.admin_uuid, tag=r.link_tag, host=aliases[0]) if aliases else None
            ),
        })
    return out


# ------------------------------ sub-resellers ------------------------------
@router.get("/subs")
async def subs(
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """The caller's DIRECT sub-resellers across all their roots, each with usage/cap/status (the
    same `node_report` the bot uses)."""
    pmap = await _panel_keys(session, {r.panel_id for r in ctx.resellers})
    out = []
    for root in ctx.resellers:
        children = (await session.execute(
            select(Reseller).where(
                Reseller.panel_id == root.panel_id,
                Reseller.parent_admin_uuid == root.admin_uuid,
            )
        )).scalars().all()
        for sub in children:
            rep = await reseller_report.node_report(session, sub, months=6)
            this_month = next(
                (m for m in rep["months"] if m["label"] == rep["current_period"]), None)
            out.append({
                "id": sub.id, "name": sub.name,
                "panel_key": pmap[sub.panel_id].key if sub.panel_id in pmap else "?",
                "parent_name": root.name,
                "users": rep["total_users"], "enabled_users": rep["enabled_users"],
                # created-users-of-subtree (rep["total_users"]) over the sub's panel quota — matches
                # the owner panel's capacity meter (replaces the meaningless active/total ratio).
                "max_users": sub.panel_max_users,
                "can_add_admin": sub.can_add_admin,
                "enforcement_state": rep["enforcement_state"],
                "gb_cap": rep["gb_cap"], "current_gb": rep["current_gb"],
                "cap_pct": rep["cap_pct"],
                "this_month_amount": this_month["amount_toman"] if this_month else 0,
                # Oldest→newest monthly sale, for the per-card trend sparkline.
                "months": [
                    {"label": m["label"], "amount_toman": m["amount_toman"], "gb": m["gb"]}
                    for m in rep["months"]
                ],
            })
    return out


# ====================== actions (P2 — full scope) ======================
async def _owns_sub(session: AsyncSession, ctx: ResellerContext, sub: Reseller) -> bool:
    """True if `sub` is a descendant of one of the caller's own reseller rows (same panel).
    Mirrors the bot's `_owns_sub` so a reseller can only act on their own subtree."""
    mine = [r for r in ctx.resellers if r.panel_id == sub.panel_id]
    for r in mine:
        if r.id == sub.id:
            continue
        if any(d.id == sub.id for d in await reseller_report.node_descendants(session, r)):
            return True
    return False


async def _notify_owner_new_payment(session: AsyncSession, result: payments.SubmitResult) -> None:
    """Send the owner the same rich review + confirm/reject buttons the bot sends. Reuses the
    bot's review builder so both surfaces stay identical (lazy import avoids load-order cost)."""
    if not result.notify or result.payment is None:
        return
    from app.bot import keyboards as kb
    from app.bot.handlers import _payment_review_html

    review = await _payment_review_html(session, result.payment, inv=result.invoice)
    await owner_notify.notify_owner(
        session, result.owner_intro + "\n\n" + review,
        html=True, reply_markup=kb.owner_payment_detail_keyboard(result.payment.id))


# ------------------------------ pay ------------------------------
@router.get("/pay/options")
async def pay_options(
    invoice_id: int,
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The enabled payment methods (+ tap-to-copy addresses) and this invoice's amounts, so the
    portal can render the pay dialog. Ownership-checked; `payable`/`pending` gate the submit."""
    inv = await session.get(Invoice, invoice_id)
    if inv is None or inv.reseller_id not in ctx.ids:
        raise HTTPException(404, "Invoice not found")
    today = tehran_today()
    payable = inv.status in _OWED and not (inv.deferred_until and inv.deferred_until > today)
    pending = await payments._pending_payment_for_invoice(session, inv.id) is not None
    opts = await payment_methods.load_options(session)
    amount_ton = None
    if opts.ton:
        from app.services import rates
        rate = await rates.get_ton_toman(session)
        if rate:
            amount_ton = round(float(inv.amount_toman) / rate, 2)
    return {
        "invoice": {
            "id": inv.id, "number": invoice_code(inv.id), "period_label": inv.period_label,
            "amount_toman": float(inv.amount_toman), "amount_usdt": float(inv.amount_usdt),
        },
        "payable": payable, "pending": pending,
        "methods": {
            "usdt": opts.usdt, "card": opts.card, "ton": opts.ton, "screenshot": opts.screenshot,
            "wallet": opts.wallet if opts.usdt else "",
            "card_number": opts.card_number if opts.card else "",
            "card_holder": opts.card_holder if opts.card else "",
            "ton_address": opts.ton_address if opts.ton else "",
            "amount_ton": amount_ton,
        },
    }


class PayTxidBody(BaseModel):
    invoice_id: int
    txid: str
    chain: str = "bsc"  # "bsc" (USDT/BEP-20) | "ton"


@router.post("/pay/txid")
async def pay_txid(
    body: PayTxidBody,
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Submit a tx hash (USDT/BSC or TON) as a PENDING payment for one owed invoice. Same shared
    validation as the bot — never auto-confirms; the owner reviews."""
    txid = (body.txid or "").strip()
    if not txid:
        raise HTTPException(400, "شناسهٔ تراکنش خالی است.")
    chain = "ton" if body.chain == "ton" else "bsc"
    result = await payments.submit_reseller_payment(
        session, reseller_ids=set(ctx.ids), invoice_id=body.invoice_id, txid=txid, chain=chain)
    await _notify_owner_new_payment(session, result)
    return {
        "status": result.status, "message": result.user_message,
        "number": payment_code(result.payment.id) if result.payment else None,
    }


@router.post("/pay/screenshot")
async def pay_screenshot(
    invoice_id: int = Form(...),
    file: UploadFile = File(...),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Submit a deposit screenshot as a PENDING payment for one owed invoice, forwarded to the
    owner for manual confirmation (same as the bot's photo path)."""
    if file.content_type and not file.content_type.startswith("image/"):
        raise HTTPException(400, "فقط تصویر قابل قبول است.")
    data = await file.read(_MAX_PROOF_BYTES + 1)
    if len(data) > _MAX_PROOF_BYTES:
        raise HTTPException(413, "حجم تصویر بیش از حد مجاز است.")
    if not data:
        raise HTTPException(400, "فایل خالی است.")
    result = await payments.submit_reseller_payment(
        session, reseller_ids=set(ctx.ids), invoice_id=invoice_id, screenshot=True)
    if result.status != "ok" or result.payment is None:
        return {"status": result.status, "message": result.user_message, "number": None}
    payment = result.payment
    proof_dir = "data/payment_proofs"
    os.makedirs(proof_dir, exist_ok=True)
    proof_path = f"{proof_dir}/payment_{payment.id}.jpg"
    try:
        with open(proof_path, "wb") as fh:
            fh.write(data)
        payment.proof_path = proof_path
        await session.commit()
    except OSError:
        pass  # keep the pending payment even if the file write fails (owner can re-request)
    from app.bot import keyboards as kb
    from app.bot.handlers import _payment_review_html

    review = await _payment_review_html(session, payment, inv=result.invoice)
    await owner_notify.notify_owner_photo(
        session, proof_path, result.owner_intro + "\n\n" + review,
        reply_markup=kb.owner_payment_detail_keyboard(payment.id))
    return {"status": "ok", "message": result.user_message, "number": payment_code(payment.id)}


# ------------------------------ sub-reseller management ------------------------------
class CapBody(BaseModel):
    gb: int


@router.post("/subs/{sub_id}/cap")
async def sub_cap(
    sub_id: int,
    body: CapBody,
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Set/clear a sub-reseller's monthly GB ceiling (0 = remove). Alert-only, no auto-suspend."""
    sub = await session.get(Reseller, sub_id)
    if sub is None or not await _owns_sub(session, ctx, sub):
        raise HTTPException(404, "Sub-reseller not found")
    if body.gb < 0:
        raise HTTPException(400, "عدد نامعتبر است.")
    sub.gb_cap = body.gb or None
    sub.gb_cap_alerted_period = None  # re-arm the alert for the new ceiling
    await session.commit()
    return {"ok": True, "gb_cap": sub.gb_cap}


@router.post("/subs/{sub_id}/suspend")
async def sub_suspend(
    sub_id: int,
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Queue a real suspension of this sub-reseller (forced write, independent of auto-dunning)."""
    sub = await session.get(Reseller, sub_id)
    if sub is None or not await _owns_sub(session, ctx, sub):
        raise HTTPException(404, "Sub-reseller not found")
    from app.services import enforcement

    action = await enforcement.enforce_reseller(session, sub, dry_run=False)
    return {"status": action.status.value, "error": action.error}


@router.post("/subs/{sub_id}/restore")
async def sub_restore(
    sub_id: int,
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Queue a restore of this sub-reseller (no-op if not suspended)."""
    sub = await session.get(Reseller, sub_id)
    if sub is None or not await _owns_sub(session, ctx, sub):
        raise HTTPException(404, "Sub-reseller not found")
    from app.services import enforcement

    action = await enforcement.queue_restore(session, sub, reason="portal")
    if action is None:
        return {"status": "not_enforced", "error": None}
    return {"status": action.status.value, "error": action.error}


# ------------------------------ support ------------------------------
class SupportBody(BaseModel):
    text: str


@router.post("/support")
async def support(
    body: SupportBody,
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Relay a message to the owner's Telegram, with a reply button that reaches the caller's
    chat. Nothing is stored — mirrors the bot's support flow."""
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "پیام خالی است.")
    text = text[:4000]
    from app.bot import keyboards as kb

    name = _html.escape(ctx.resellers[0].name or str(ctx.chat_id))
    msg = (
        "💬 پیام پشتیبانی (از پنلِ وب)\n"
        f"از: <a href='tg://user?id={ctx.chat_id}'>{name}</a> (id: <code>{ctx.chat_id}</code>)\n\n"
        f"{_html.escape(text)}"
    )
    sent = await owner_notify.notify_owner(
        session, msg, html=True, reply_markup=kb.support_reply_keyboard(ctx.chat_id, 0))
    if not sent:
        raise HTTPException(503, "در حال حاضر پشتیبانی در دسترس نیست؛ بعداً تلاش کنید.")
    return {"ok": True}


# ------------------------------ sub-reseller PDF + capacity ------------------------------
@router.get("/subs/{sub_id}/pdf")
async def sub_pdf(
    sub_id: int,
    period: str | None = None,
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """A GB-only invoice PDF for one of the caller's sub-resellers (sub + its subtree) for a month
    — so a reseller can bill their sub. Reuses the bot's `render_sub_invoice_pdf`."""
    sub = await session.get(Reseller, sub_id)
    if sub is None or not await _owns_sub(session, ctx, sub):
        raise HTTPException(404, "Sub-reseller not found")
    try:
        p = parse_period(period) if period else current_month()
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "دورهٔ نامعتبر است.") from exc
    issuer = next((r.name for r in ctx.resellers if r.panel_id == sub.panel_id), ctx.resellers[0].name)
    result = await invoice_pdf_service.render_sub_invoice_pdf(session, sub, p, issuer_name=issuer or "")
    if result is None:
        raise HTTPException(404, "برای این دوره فروشی ثبت نشده است.")
    path, filename = result
    return FileResponse(path, media_type="application/pdf", filename=filename)


@router.get("/subs/{sub_id}/sales-by-day")
async def sub_sales_by_day(
    sub_id: int,
    period: str | None = None,
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """One of the caller's sub-resellers' daily sale trend for a month (sub + its subtree): one bucket
    per day, height = that day's share by each service's creation date. Same shape/logic as the owner
    dashboard's sales-by-day and the portal `/summary` trend."""
    sub = await session.get(Reseller, sub_id)
    if sub is None or not await _owns_sub(session, ctx, sub):
        raise HTTPException(404, "Sub-reseller not found")
    try:
        p = parse_period(period) if period else current_month()
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, "دورهٔ نامعتبر است.") from exc
    n_days = p.end.day
    buckets = [0.0] * n_days
    bundle = await reseller_report.node_invoice(session, sub, p)
    if bundle:
        for line in bundle.lines:
            if line.start_date and p.contains(line.start_date):
                buckets[line.start_date.day - 1] += line.usage_gb * bundle.price_per_gb
    return [
        {"day": i + 1,
         "date": dt.date(p.start.year, p.start.month, i + 1).isoformat(),
         "amount_toman": round(v)}
        for i, v in enumerate(buckets)
    ]


class BumpBody(BaseModel):
    amount: int


@router.post("/subs/{sub_id}/bump-limits")
async def sub_bump_limits(
    sub_id: int,
    body: BumpBody,
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Increase a sub-reseller's max_users / max_active_users on the panel (reseller-initiated)."""
    sub = await session.get(Reseller, sub_id)
    if sub is None or not await _owns_sub(session, ctx, sub):
        raise HTTPException(404, "Sub-reseller not found")
    if not (0 < body.amount <= 5000):
        raise HTTPException(400, "مقدار باید بین ۱ تا ۵۰۰۰ باشد.")
    from app.services import admin_capacity

    try:
        new_mu, new_mau = await admin_capacity.bump_limits(session, sub, body.amount)
    except Exception as exc:  # noqa: BLE001 — surface a clean panel-API error to the client
        raise HTTPException(502, f"خطا در ارتباط با پنل: {exc}") from exc
    return {"max_users": new_mu, "max_active_users": new_mau}


class CanAddAdminBody(BaseModel):
    enabled: bool


@router.post("/subs/{sub_id}/can-add-admin")
async def sub_can_add_admin(
    sub_id: int,
    body: CanAddAdminBody,
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Toggle a sub-reseller's ability to create its own sub-admins on the panel."""
    sub = await session.get(Reseller, sub_id)
    if sub is None or not await _owns_sub(session, ctx, sub):
        raise HTTPException(404, "Sub-reseller not found")
    from app.services import admin_capacity

    try:
        await admin_capacity.set_can_add_admin(session, sub, body.enabled)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"خطا در ارتباط با پنل: {exc}") from exc
    return {"ok": True, "can_add_admin": body.enabled}


# ------------------------------ my capacity ------------------------------
@router.get("/capacity")
async def capacity(
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    """How full each of the caller's OWN reseller quotas is — created users (whole subtree) over the
    panel max_users, same definition as the owner panel's capacity meter."""
    pmap = await _panel_keys(session, {r.panel_id for r in ctx.resellers})
    out = []
    for r in ctx.resellers:
        rep = await reseller_report.node_report(session, r, months=1)
        out.append({
            "reseller_id": r.id, "name": r.name,
            "panel_key": pmap[r.panel_id].key if r.panel_id in pmap else "?",
            "used": rep["total_users"], "max": r.panel_max_users,
            "can_add_admin": r.can_add_admin,
        })
    return out


class CapacityRequestBody(BaseModel):
    reseller_id: int
    amount: int | None = None
    note: str | None = None


@router.post("/capacity/request")
async def capacity_request(
    body: CapacityRequestBody,
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Ask the owner (via Telegram) to raise this reseller's capacity. Nothing is stored."""
    if body.reseller_id not in ctx.ids:
        raise HTTPException(404, "Reseller not found")
    r = next(x for x in ctx.resellers if x.id == body.reseller_id)
    pmap = await _panel_keys(session, {r.panel_id})
    panel = pmap.get(r.panel_id)
    rep = await reseller_report.node_report(session, r, months=1)
    name = _html.escape(r.name or str(ctx.chat_id))
    max_txt = str(r.panel_max_users) if r.panel_max_users is not None else "∞"
    amount_txt = (
        f"\nمقدارِ درخواستی: {int(body.amount)}" if body.amount and body.amount > 0 else ""
    )
    note_txt = f"\nیادداشت: {_html.escape(body.note[:500])}" if body.note else ""
    msg = (
        "📈 درخواستِ افزایشِ ظرفیت (از پنلِ وب)\n"
        f"نماینده: <a href='tg://user?id={ctx.chat_id}'>{name}</a> (id: <code>{ctx.chat_id}</code>)\n"
        f"پنل: {_html.escape(panel.key if panel else '—')}\n"
        f"ظرفیتِ فعلی: {rep['total_users']}/{max_txt}{amount_txt}{note_txt}"
    )
    from app.bot import keyboards as kb

    sent = await owner_notify.notify_owner(
        session, msg, html=True,
        reply_markup=kb.capacity_request_keyboard(r.id, int(body.amount or 0)))
    if not sent:
        raise HTTPException(503, "در حال حاضر در دسترس نیست؛ بعداً تلاش کنید.")
    return {"ok": True}


# ------------------------------ notifications (derived, no table) ------------------------------
@router.get("/notifications")
async def notifications(
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """A merged, newest-first feed of recent events for the caller — derived live from existing data
    (no notifications table). The client tracks 'seen' via a localStorage timestamp."""
    events: list[dict] = []

    invs = (await session.execute(
        select(Invoice)
        .where(Invoice.reseller_id.in_(ctx.ids), Invoice.status != InvoiceStatus.draft)
        .order_by(Invoice.id.desc()).limit(10)
    )).scalars().all()
    for i in invs:
        at = i.sent_at or i.created_at
        events.append({
            "key": f"inv-{i.id}", "type": "invoice",
            "at": at.isoformat() if at else None,
            "title": f"فاکتور دورهٔ {i.period_label} صادر شد",
            "severity": "info",
        })

    pays = (await session.execute(
        select(Payment).where(Payment.reseller_id.in_(ctx.ids))
        .order_by(Payment.id.desc()).limit(10)
    )).scalars().all()
    for p in pays:
        st = p.status.value
        if st == "confirmed":
            title, sev = f"پرداختِ #{payment_code(p.id)} تأیید شد", "success"
        elif st == "rejected":
            title, sev = f"پرداختِ #{payment_code(p.id)} رد شد", "error"
        else:
            title, sev = f"پرداختِ #{payment_code(p.id)} در انتظارِ تأیید است", "info"
        at = p.verified_at or p.created_at
        events.append({
            "key": f"pay-{p.id}", "type": "payment",
            "at": at.isoformat() if at else None, "title": title, "severity": sev,
        })

    for r in ctx.resellers:
        if r.enforcement_state.value == "enforced":
            events.append({
                "key": f"enf-{r.id}", "type": "enforcement", "at": None,
                "title": f"نمایندگیِ «{r.name}» مسدود است", "severity": "error",
            })

    # Direct subs that hit their monthly GB cap this period.
    cur = current_month().label
    my_uuids = {r.admin_uuid for r in ctx.resellers}
    my_panels = {r.panel_id for r in ctx.resellers}
    valid_pairs = {(r.panel_id, r.admin_uuid) for r in ctx.resellers}
    if my_uuids and my_panels:
        cap_subs = (await session.execute(
            select(Reseller).where(
                Reseller.panel_id.in_(my_panels),
                Reseller.parent_admin_uuid.in_(my_uuids),
                Reseller.gb_cap_alerted_period == cur,
            )
        )).scalars().all()
        for s in cap_subs:
            if (s.panel_id, s.parent_admin_uuid) in valid_pairs:
                events.append({
                    "key": f"cap-{s.id}", "type": "cap", "at": None,
                    "title": f"زیرمجموعهٔ «{s.name}» به سقفِ حجمِ ماهانه رسید",
                    "severity": "warning",
                })

    events.sort(key=lambda e: (e["at"] or ""), reverse=True)
    return {"events": events[:20]}
