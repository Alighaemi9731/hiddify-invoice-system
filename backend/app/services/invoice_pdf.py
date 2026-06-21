"""Render an invoice to a PDF file (shared by the API download + the bot delivery)."""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.codes import invoice_code
from app.models import Invoice, InvoiceLine, Panel, Reseller
from app.services import pdf as pdf_service
from app.services import settings_service


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
    pdf_service.build_invoice_pdf(
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
    pdf_service.build_invoice_pdf(
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
    pdf_service.build_invoice_pdf(
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
    pdf_service.build_invoice_pdf(
        out_path,
        reseller_name=node.name, panel_label=panel.key if panel else "",
        period_label=period.label, period_start=period.start, period_end=period.end,
        lines=lines,
        total_gb=total_gb,
        owner_name=issuer_name,  # the issuing reseller, not the panel owner
    )
    return out_path, f"factor_{safe}_{period.label}.pdf"
