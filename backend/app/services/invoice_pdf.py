"""Render an invoice to a PDF file (shared by the API download + the bot delivery).

Every reportlab render goes through `_build_pdf`, which runs the synchronous rendering in
a worker thread — monthly generation renders N+1 PDFs per reseller, and doing that on the
event loop stalls every concurrent API/bot request. Font registration is serialized inside
`pdf._register_fonts`, and each render builds its own document, so parallel renders are safe.
"""
from __future__ import annotations

import asyncio
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import invoice_code
from app.models import Invoice, InvoiceLine, Panel, Reseller
from app.services import pdf as pdf_service
from app.services import settings_service


async def _build_pdf(out_path: str, **kwargs) -> None:
    """Run the synchronous reportlab render off the event loop."""
    await asyncio.to_thread(pdf_service.build_invoice_pdf, out_path, **kwargs)


def _safe_name(name: str) -> str:
    # Keep Persian/Latin/digits, drop emoji & filesystem-unfriendly chars.
    s = re.sub(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF]+", "", name or "")
    s = re.sub(r"[^\w؀-ۿ \-]", "", s).strip().replace(" ", "_")
    return s[:40] or "reseller"


async def render_invoice_pdf(session: AsyncSession, inv: Invoice) -> tuple[str, str]:
    """Build the PDF for an invoice, store its path, and return (path, download_name)."""
    reseller = await session.get(Reseller, inv.reseller_id)
    panel = await session.get(Panel, inv.panel_id)
    if reseller is None or panel is None:
        raise ValueError("invoice references a missing reseller or panel")
    lines = (
        await session.execute(
            select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
            .order_by(InvoiceLine.usage_gb.desc())
        )
    ).scalars().all()
    owner_name = await settings_service.get(session, "owner_name", "") or ""

    safe = _safe_name(reseller.name)
    # Disk path carries the invoice id so two resellers with the SAME name in a period don't
    # overwrite each other's file (the Telegram filename below stays clean/human).
    out_path = f"data/invoices/{inv.period_label}/factor_{safe}_{inv.id}_{inv.period_label}.pdf"
    await _build_pdf(
        out_path,
        reseller_name=reseller.name, panel_label=panel.key, period_label=inv.period_label,
        period_start=inv.period_start, period_end=inv.period_end,
        lines=[
            {"name": line.name, "uuid": line.end_user_uuid, "start_date": line.start_date,
             "usage_gb": float(line.usage_gb),
             "sub_reseller_name": line.sub_reseller_name or reseller.name}
            for line in lines
        ],
        total_gb=float(inv.usage_gb),
        owner_name=owner_name,
        invoice_no=invoice_code(inv.id),
    )
    inv.pdf_path = out_path
    await session.commit()
    return out_path, f"factor_{safe}_{inv.period_label}.pdf"


async def _grouped_invoice_lines(
    session: AsyncSession, inv: Invoice, reseller: Reseller
) -> list[dict]:
    """Group a persisted invoice's lines by node — the reseller's OWN users first, then one group
    per sub-reseller — for the per-node PDFs/breakdown. The grouping key is the UNIQUE creator uuid
    (`added_by_uuid`), NOT the display name, so two resellers that happen to share a name are never
    merged; the display name is resolved from the panel's uuid→name map (falling back to the line's
    recorded `sub_reseller_name`). Every line lands in exactly one group, and the groups are then
    RECONCILED to the locked `inv.usage_gb` (a manual `usage_gb` edit doesn't touch lines) with a
    transparent adjustment line so the PDFs + breakdown always sum to the amount shown in the text.
    Returns [{name, is_own, lines:[dict]}, …] (each line a build_invoice_pdf-ready dict)."""
    rows_lines = (
        await session.execute(
            select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
            .order_by(InvoiceLine.usage_gb.desc())
        )
    ).scalars().all()
    if not rows_lines:
        return []
    rows = (
        await session.execute(
            select(Reseller.admin_uuid, Reseller.name).where(Reseller.panel_id == inv.panel_id)
        )
    ).all()
    name_by_uuid = {(u or "").lower(): n for u, n in rows}
    own_key = (reseller.admin_uuid or "").lower()
    own_name = reseller.name or ""
    groups: dict[str, dict] = {}
    for ln in rows_lines:
        key = (ln.added_by_uuid or "").lower() or own_key
        g = groups.get(key)
        if g is None:
            is_own = key == own_key
            nm = own_name if is_own else (
                name_by_uuid.get(key) or (ln.sub_reseller_name or "").strip() or "—")
            g = groups[key] = {"name": nm, "is_own": is_own, "lines": []}
        g["lines"].append({
            "name": ln.name, "uuid": ln.end_user_uuid, "start_date": ln.start_date,
            "usage_gb": float(ln.usage_gb), "sub_reseller_name": ln.sub_reseller_name or g["name"],
        })
    # Reconcile to the locked total: if a manual usage_gb edit made the lines no longer sum to it,
    # add a clear adjustment line to the own group so text == breakdown == PDFs.
    line_sum = round(sum(row["usage_gb"] for g in groups.values() for row in g["lines"]), 3)
    diff = round(float(inv.usage_gb) - line_sum, 3)
    if abs(diff) > 0.01:
        own = groups.get(own_key) or groups.setdefault(
            own_key, {"name": own_name, "is_own": True, "lines": []})
        own["lines"].append({"name": "تعدیل فاکتور", "uuid": "", "start_date": None,
                             "usage_gb": diff, "sub_reseller_name": own["name"]})
    ordered = [own_key] + sorted(k for k in groups if k != own_key)
    return [groups[k] for k in ordered if k in groups]


async def render_invoice_node_pdfs(
    session: AsyncSession, inv: Invoice, reseller: Reseller
) -> list[tuple[str, str]]:
    """Render the per-node, volume-only PDFs (own + one per sub-reseller) for a PERSISTED invoice
    straight from its stored `InvoiceLine` rows — NOT a live recompute. This guarantees the PDFs
    match the locked invoice text: a user deleted from the panel (or a quota changed) AFTER the
    invoice was issued can no longer silently shrink the PDF. The grand total across the returned
    PDFs equals `inv.usage_gb`. Returns [(path, caption), …] (empty → caller falls back)."""
    grouped = await _grouped_invoice_lines(session, inv, reseller)
    if not grouped:
        return []
    panel = await session.get(Panel, inv.panel_id)
    owner_name = await settings_service.get(session, "owner_name", "") or ""
    title = f"فاکتور دوره {inv.period_label}"
    docs: list[tuple[str, str]] = []
    for idx, g in enumerate(grouped):
        node_name, is_own, gl = g["name"], g["is_own"], g["lines"]
        total = round(sum(float(ln["usage_gb"]) for ln in gl), 3)
        safe = _safe_name(node_name)
        out_path = f"data/invoices/{inv.period_label}/inv{inv.id}_n{idx}_{safe}_{inv.period_label}.pdf"
        await _build_pdf(
            out_path,
            reseller_name=node_name, panel_label=panel.key if panel else "",
            period_label=inv.period_label, period_start=inv.period_start, period_end=inv.period_end,
            lines=gl,
            total_gb=total, owner_name=owner_name, invoice_title=title,
            invoice_no=invoice_code(inv.id) if is_own else "",
        )
        caption = (
            f"📄 {title} — کاربران خودتان" if is_own
            else f"📄 {title} — زیرمجموعه «{node_name}»"
        )
        docs.append((out_path, caption))
    return docs


async def invoice_node_breakdown(
    session: AsyncSession, inv: Invoice, reseller: Reseller
) -> dict | None:
    """The per-node usage breakdown (own + subs) built from a PERSISTED invoice's lines, matching
    the shape of `reseller_report.interim_breakdown` so the invoice text breakdown is authoritative
    (sums to the locked total) instead of a live recompute. None when the invoice has no lines."""
    grouped = await _grouped_invoice_lines(session, inv, reseller)
    if not grouped:
        return None
    own = {"gb": 0.0, "users": 0}
    subs: list[dict] = []
    for g in grouped:
        gb = round(sum(float(ln["usage_gb"]) for ln in g["lines"]), 3)
        entry = {"gb": gb, "users": len(g["lines"])}
        if g["is_own"]:
            own = entry
        else:
            subs.append({"name": g["name"], **entry})
    total = round(sum(float(ln["usage_gb"]) for g in grouped for ln in g["lines"]), 3)
    return {"own": own, "subs": subs, "total_gb": total}


async def render_node_usage_pdf(
    session: AsyncSession, node: Reseller, period, *, title: str = "فاکتور", issuer_name: str = "",
) -> tuple[str, str] | None:
    """Render a volume-only usage PDF for a node + its whole subtree (the same bundle the
    real monthly invoice covers), for any period. Used for the reseller's own interim
    invoice. Includes the abuse-metered extra so it matches the text + the real invoice.
    Returns None if there's zero billable usage in the period."""
    from app.services.reseller_report import node_invoice_pdf_lines

    result = await node_invoice_pdf_lines(session, node, period, own_only=False)
    if result is None:
        return None
    lines, total_gb = result
    panel = await session.get(Panel, node.panel_id)
    owner_name = issuer_name or (await settings_service.get(session, "owner_name", "") or "")
    safe = _safe_name(node.name)
    out_path = f"data/invoices/{period.label}/usage_{safe}_{node.id}_{period.label}.pdf"
    await _build_pdf(
        out_path,
        reseller_name=node.name, panel_label=panel.key if panel else "",
        period_label=period.label, period_start=period.start, period_end=period.end,
        lines=lines,
        total_gb=total_gb,
        owner_name=owner_name, invoice_title=title,
    )
    return out_path, f"{title.replace(' ', '_')}_{safe}_{period.label}.pdf"


async def render_own_usage_pdf(
    session: AsyncSession, node: Reseller, period, *, title: str = "فاکتور", issuer_name: str = "",
) -> tuple[str, str] | None:
    """Render a volume-only usage PDF for a node's OWN users only (just `node.admin_uuid`,
    NOT its subtree) — so a top reseller's interim invoice lists only the users they created
    themselves, exactly like each sub-reseller gets its own separate PDF. Includes the
    abuse-metered extra so it matches the text + the real invoice. Returns None if there's
    zero billable own usage in the period."""
    from app.services.reseller_report import node_invoice_pdf_lines

    result = await node_invoice_pdf_lines(session, node, period, own_only=True)
    if result is None:
        return None
    lines, total_gb = result
    panel = await session.get(Panel, node.panel_id)
    owner_name = issuer_name or (await settings_service.get(session, "owner_name", "") or "")
    safe = _safe_name(node.name)
    out_path = f"data/invoices/{period.label}/own_{safe}_{node.id}_{period.label}.pdf"
    await _build_pdf(
        out_path,
        reseller_name=node.name, panel_label=panel.key if panel else "",
        period_label=period.label, period_start=period.start, period_end=period.end,
        lines=lines,
        total_gb=total_gb,
        owner_name=owner_name, invoice_title=title,
    )
    return out_path, f"{title.replace(' ', '_')}_own_{safe}_{period.label}.pdf"


async def render_sub_invoice_pdf(
    session: AsyncSession, node: Reseller, period, *, issuer_name: str = ""
) -> tuple[str, str] | None:
    """Render an on-demand invoice PDF for ONE sub-reseller (node + its own subtree) for
    a period, so a reseller can bill their sub-resellers. Includes the abuse-metered extra so
    it matches the text + the real invoice. NOT persisted as an Invoice (the owner's invoice
    already covers the subtree). Returns None if zero billable usage."""
    from app.services.reseller_report import node_invoice_pdf_lines

    result = await node_invoice_pdf_lines(session, node, period, own_only=False)
    if result is None:
        return None
    lines, total_gb = result

    panel = await session.get(Panel, node.panel_id)
    safe = _safe_name(node.name)
    out_path = f"data/invoices/{period.label}/sub_{safe}_{node.id}_{period.label}.pdf"
    await _build_pdf(
        out_path,
        reseller_name=node.name, panel_label=panel.key if panel else "",
        period_label=period.label, period_start=period.start, period_end=period.end,
        lines=lines,
        total_gb=total_gb,
        owner_name=issuer_name,  # the issuing reseller, not the panel owner
    )
    return out_path, f"factor_{safe}_{period.label}.pdf"
