"""
Find end-users created with a very large SOLD quota (the 1000 GB Hiddify default left by mistake)
and attribute each to the top-level reseller whose invoice it will land on — so the owner can warn
that reseller before the month is billed.

The creator→root mapping, the present-filter, and the «current month» rule deliberately mirror the
invoice engine (invoicing._panel_billable / _reseller_present + invoice_engine roots), so this list
matches exactly what would actually be billed. Nothing is written to the database.
"""
from __future__ import annotations

import html as _html
from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BotUser, EndUserSnapshot, Panel, Reseller
from app.services import invoicing, pricing
from app.services.broadcast import Recipient
from app.services.invoice_engine import (
    build_children_map,
    collect_descendants,
    select_billable_roots,
)
from app.services.periods import current_month


def _iso(value: object) -> str:
    """First-Strong Isolate so an English name renders cleanly inside an RTL line."""
    return f"⁨{value}⁩"


async def high_volume_users(
    session: AsyncSession, *, threshold: float, panel_id: int | None = None,
    this_month_only: bool = True,
) -> list[dict[str, Any]]:
    """Rows of high-volume users (usage_limit_gb >= threshold) with the responsible top-level
    reseller. Only users whose creator sits under a present, billable root are included."""
    default_price = await pricing.get_default_price_per_gb(session)
    period = current_month()

    panel_q = select(Panel).where(Panel.enabled.is_(True))
    if panel_id is not None:
        panel_q = select(Panel).where(Panel.id == panel_id)
    panels = (await session.execute(panel_q)).scalars().all()

    rows: list[dict[str, Any]] = []
    chat_ids: list[int] = []
    for panel in panels:
        ok, _ = invoicing._panel_billable(panel)
        if not ok:
            continue
        resellers = (await session.execute(
            select(Reseller).where(Reseller.panel_id == panel.id)
        )).scalars().all()
        children = build_children_map(resellers)
        by_uuid = {(r.admin_uuid or "").lower(): r for r in resellers}
        # creator(lower) -> responsible root, ONLY for present (billable) roots' subtrees.
        creator_to_root: dict[str, Reseller] = {}
        for root in select_billable_roots(resellers):
            if not invoicing._reseller_present(root, panel):
                continue
            for d in collect_descendants(root, children):
                creator_to_root[(d.admin_uuid or "").lower()] = root

        users = (await session.execute(
            select(EndUserSnapshot).where(
                EndUserSnapshot.panel_id == panel.id,
                EndUserSnapshot.usage_limit_gb >= threshold,
                EndUserSnapshot.added_by_uuid.is_not(None),
            )
        )).scalars().all()
        for u in users:
            in_month = period.contains(u.start_date)
            if this_month_only and not in_month:
                continue
            creator = (u.added_by_uuid or "").lower()
            root = creator_to_root.get(creator)
            if root is None:
                continue  # created by the Owner or a removed reseller → not billed to anyone here
            creator_res = by_uuid.get(creator)
            eff_price = int(root.price_per_gb or default_price)
            gb = float(u.usage_limit_gb or 0)
            if root.bot_chat_id:
                chat_ids.append(root.bot_chat_id)
            rows.append({
                "user_uuid": (u.user_uuid or "")[:8],
                "name": u.name or "",
                "usage_limit_gb": gb,
                "start_date": u.start_date.isoformat() if u.start_date else None,
                "in_current_month": in_month,
                "panel_key": panel.key,
                "creator_name": (creator_res.name if creator_res else "—") or "—",
                "creator_is_sub": creator != (root.admin_uuid or "").lower(),
                "root_reseller_id": root.id,
                "root_name": root.name or "—",
                "root_bot_chat_id": root.bot_chat_id,
                "root_username": None,
                "effective_price_per_gb": eff_price,
                "would_be_toman": round(gb * eff_price),
            })

    usernames: dict[int, str] = {}
    if chat_ids:
        usernames = {
            tid: uname for tid, uname in (await session.execute(
                select(BotUser.telegram_id, BotUser.username).where(
                    BotUser.telegram_id.in_(chat_ids), BotUser.username.is_not(None))
            )).all()
        }
    for row in rows:
        if row["root_bot_chat_id"]:
            row["root_username"] = usernames.get(row["root_bot_chat_id"])
    rows.sort(key=lambda r: r["would_be_toman"], reverse=True)
    return rows


def _warn_text(items: list[dict[str, Any]]) -> str:
    """Aggregated warning for ONE reseller listing all their high-volume users (HTML, bidi-safe)."""
    from app.bot.rtl import rtl

    lines = [
        "⚠️ بررسیِ حجمِ کاربرانِ شما",
        "",
        "نمایندهٔ گرامی، چند کاربر با حجمِ بسیار بالا در پنلِ شما ساخته شده که اگر اصلاح نشود، "
        "فاکتورِ این ماهِ شما را سنگین می‌کند:",
        "─────────────────",
    ]
    for it in items:
        gb = f"{it['usage_limit_gb']:,.0f}"
        toman = f"{it['would_be_toman']:,.0f}"
        name = _iso(_html.escape(it["name"] or "بدون‌نام"))
        lines.append(f"‏👤 {name} — حجم: {gb} گیگ ≈ {toman} تومان")
        if it["creator_is_sub"]:
            lines.append(f"‏   ↳ ساختهٔ زیرمجموعهٔ شما: {_iso(_html.escape(it['creator_name']))}")
    lines.append("─────────────────")
    lines.append("")
    lines.append(
        "ℹ️ فاکتور بر اساسِ «حجمِ فروخته‌شدهٔ» هر کاربر محاسبه می‌شود، نه مصرف. حجمِ پیش‌فرضِ "
        "ساختِ کاربر در پنل ۱۰۰۰ گیگ است؛ اگر اشتباه ساخته‌اید، همین حالا حجمِ کاربر را اصلاح "
        "کنید تا از فاکتورِ سنگین جلوگیری شود."
    )
    return rtl("\n".join(lines))


async def build_warnings(
    session: AsyncSession, *, threshold: float, panel_id: int | None,
    this_month_only: bool, root_reseller_ids: list[int] | None,
) -> tuple[list[Recipient], dict[int, str], int]:
    """(reachable recipients, per-root warning texts, unregistered count) — grouped by responsible
    root. Optionally restricted to specific roots (single-row «warn this admin»)."""
    rows = await high_volume_users(
        session, threshold=threshold, panel_id=panel_id, this_month_only=this_month_only)
    by_root: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_root[r["root_reseller_id"]].append(r)
    if root_reseller_ids is not None:
        wanted = set(root_reseller_ids)
        by_root = {rid: items for rid, items in by_root.items() if rid in wanted}

    reachable: list[Recipient] = []
    texts: dict[int, str] = {}
    unregistered = 0
    for rid, items in by_root.items():
        chat_id = items[0]["root_bot_chat_id"]
        if not chat_id:
            unregistered += 1
            continue
        texts[rid] = _warn_text(items)
        reachable.append(Recipient(
            reseller_id=rid, name=items[0]["root_name"], panel=items[0]["panel_key"],
            chat_id=chat_id, status="pending"))
    return reachable, texts, unregistered
