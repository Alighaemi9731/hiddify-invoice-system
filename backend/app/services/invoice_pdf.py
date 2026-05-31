"""Render an invoice to a PDF file (shared by the API download + the bot delivery)."""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invoice, InvoiceLine, Panel, Reseller
from app.services import pdf as pdf_service, settings_service


def _safe_name(name: str) -> str:
    # Keep Persian/Latin/digits, drop emoji & filesystem-unfriendly chars.
    s = re.sub(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF]+", "", name or "")
    s = re.sub(r"[^\w؀-ۿ \-]", "", s).strip().replace(" ", "_")
    return s[:40] or "reseller"


async def render_invoice_pdf(session: AsyncSession, inv: Invoice) -> tuple[str, str]:
    """Build the PDF for an invoice, store its path, and return (path, download_name)."""
    reseller = await session.get(Reseller, inv.reseller_id)
    panel = await session.get(Panel, inv.panel_id)
    lines = (
        await session.execute(
            select(InvoiceLine).where(InvoiceLine.invoice_id == inv.id)
            .order_by(InvoiceLine.usage_gb.desc())
        )
    ).scalars().all()
    wallet = await settings_service.get(session, "usdt_bep20_address", "") or ""
    owner_name = await settings_service.get(session, "owner_name", "") or ""

    safe = _safe_name(reseller.name)
    # Clean, human filename: factor_<name>_<period>.pdf  (no invoice id).
    out_path = f"data/invoices/{inv.period_label}/factor_{safe}_{inv.period_label}.pdf"
    pdf_service.build_invoice_pdf(
        out_path,
        reseller_name=reseller.name, panel_label=panel.key, period_label=inv.period_label,
        period_start=inv.period_start, period_end=inv.period_end,
        lines=[
            {"name": l.name, "uuid": l.end_user_uuid, "start_date": l.start_date,
             "usage_gb": float(l.usage_gb), "sub_reseller_name": l.sub_reseller_name or reseller.name}
            for l in lines
        ],
        total_gb=float(inv.usage_gb), price_per_gb=inv.price_per_gb,
        amount_toman=float(inv.amount_toman), amount_usdt=float(inv.amount_usdt),
        usdt_rate=int(inv.usdt_rate), wallet_address=wallet,
        base_amount_toman=float(inv.base_amount_toman or inv.amount_toman),
        min_sale_toman=int(inv.min_sale_toman or 0), floor_applied=bool(inv.floor_applied),
        owner_name=owner_name,
    )
    inv.pdf_path = out_path
    await session.commit()
    return out_path, f"factor_{safe}_{inv.period_label}.pdf"


async def render_sub_invoice_pdf(
    session: AsyncSession, node: Reseller, period, *, issuer_name: str = ""
) -> tuple[str, str] | None:
    """Render an on-demand invoice PDF for ONE sub-reseller (node + its own subtree) for
    a period, so a reseller can bill their sub-resellers. NOT persisted as an Invoice
    (the owner's invoice already covers the subtree). Returns None if zero billable usage.
    """
    from app.services import pricing
    from app.services.reseller_report import node_invoice

    bundle = await node_invoice(session, node, period)
    if bundle is None or bundle.total_gb <= 0:
        return None

    panel = await session.get(Panel, node.panel_id)
    rate = await pricing.get_rate(session)
    amount_toman = bundle.amount_toman
    amount_usdt = float(pricing.toman_to_usdt(amount_toman, rate))
    safe = _safe_name(node.name)
    out_path = f"data/invoices/{period.label}/sub_{safe}_{period.label}.pdf"
    pdf_service.build_invoice_pdf(
        out_path,
        reseller_name=node.name, panel_label=panel.key if panel else "",
        period_label=period.label, period_start=period.start, period_end=period.end,
        lines=[
            {"name": l.name, "uuid": l.user_uuid, "start_date": l.start_date,
             "usage_gb": float(l.usage_gb), "sub_reseller_name": l.sub_reseller_name or node.name}
            for l in bundle.lines
        ],
        total_gb=float(bundle.total_gb), price_per_gb=bundle.price_per_gb,
        amount_toman=float(amount_toman), amount_usdt=amount_usdt,
        usdt_rate=int(rate), wallet_address="",  # reseller↔sub settlement is off-system
        base_amount_toman=float(bundle.base_amount_toman or amount_toman),
        min_sale_toman=int(bundle.min_sale_toman or 0), floor_applied=bool(bundle.floor_applied),
        owner_name=issuer_name,  # the issuing reseller, not the panel owner
    )
    return out_path, f"factor_{safe}_{period.label}.pdf"
