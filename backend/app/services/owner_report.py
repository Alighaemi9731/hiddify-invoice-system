"""
Owner-facing reporting for the admin bot and the scheduled daily digest.

Pure data (dataclasses) + Persian render helpers, so the bot handlers and the
`daily_digest_job` share one source of truth for the numbers AND their wording. All
money is Toman; the period is a Gregorian month label ("YYYY-MM"). Read-only.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EnforcementAction, Invoice, Panel, Payment, Reseller
from app.models.enums import (
    EnforcementActionStatus,
    InvoiceStatus,
    PanelStatus,
    PaymentStatus,
)
from app.services import settings_service
from app.services.periods import Period, current_month, parse_period
from app.services.periods import today as tehran_today

_OWED = (InvoiceStatus.sent, InvoiceStatus.overdue, InvoiceStatus.enforced)
_DELIVERED = (
    InvoiceStatus.sent,
    InvoiceStatus.overdue,
    InvoiceStatus.enforced,
    InvoiceStatus.paid,
)
_LIVE_ACTIONS = (EnforcementActionStatus.planned, EnforcementActionStatus.partial)

try:
    from zoneinfo import ZoneInfo

    _TZ = ZoneInfo("Asia/Tehran")
except Exception:  # pragma: no cover
    _TZ = None  # type: ignore


def _toman(n) -> str:
    return f"{float(n or 0):,.0f}"


def _rtl(line: str) -> str:
    """Prefix a right-to-left mark so a line that starts with a Latin name/number still
    renders right-aligned in Telegram."""
    return "‏" + line


def _prev_label(label: str) -> str:
    p = parse_period(label)
    first = p.start.replace(day=1)
    prev_last = first - dt.timedelta(days=1)
    return f"{prev_last.year:04d}-{prev_last.month:02d}"


def _today_start_utc() -> dt.datetime:
    d = tehran_today()
    if _TZ is not None:
        return dt.datetime(d.year, d.month, d.day, tzinfo=_TZ).astimezone(dt.timezone.utc)
    return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc)


# ----------------------------- period KPIs -----------------------------
@dataclass
class PeriodStats:
    label: str
    billed: float = 0.0          # delivered invoices' total for the period
    collected: float = 0.0       # paid invoices' total
    outstanding: float = 0.0     # still-owed total FOR THIS period
    invoices: int = 0
    paid: int = 0
    debtors: int = 0             # distinct resellers owed for this period
    services: int = 0            # billed services (sum of users_count)
    prev_billed: float = 0.0
    prev_collected: float = 0.0
    total_outstanding: float = 0.0  # all-time owed (every period)

    @property
    def collection_rate(self) -> float:
        return round(self.collected / self.billed * 100, 1) if self.billed > 0 else 0.0


async def _sum(session: AsyncSession, *conds) -> float:
    q = select(func.coalesce(func.sum(Invoice.amount_toman), 0)).where(*conds)
    return float((await session.execute(q)).scalar_one() or 0)


async def _count(session: AsyncSession, *conds) -> int:
    q = select(func.count(Invoice.id)).where(*conds)
    return int((await session.execute(q)).scalar_one() or 0)


async def period_stats(session: AsyncSession, label: str) -> PeriodStats:
    in_period = Invoice.period_label == label
    s = PeriodStats(label=label)
    s.billed = await _sum(session, in_period, Invoice.status.in_(_DELIVERED))
    s.collected = await _sum(session, in_period, Invoice.status == InvoiceStatus.paid)
    s.outstanding = await _sum(session, in_period, Invoice.status.in_(_OWED))
    s.invoices = await _count(session, in_period, Invoice.status.in_(_DELIVERED))
    s.paid = await _count(session, in_period, Invoice.status == InvoiceStatus.paid)
    s.debtors = int(
        (
            await session.execute(
                select(func.count(func.distinct(Invoice.reseller_id))).where(
                    in_period, Invoice.status.in_(_OWED)
                )
            )
        ).scalar_one()
        or 0
    )
    s.services = int(
        (
            await session.execute(
                select(func.coalesce(func.sum(Invoice.users_count), 0)).where(
                    in_period, Invoice.status.in_(_DELIVERED)
                )
            )
        ).scalar_one()
        or 0
    )
    prev = _prev_label(label)
    s.prev_billed = await _sum(session, Invoice.period_label == prev, Invoice.status.in_(_DELIVERED))
    s.prev_collected = await _sum(
        session, Invoice.period_label == prev, Invoice.status == InvoiceStatus.paid
    )
    s.total_outstanding = await _sum(session, Invoice.status.in_(_OWED))
    return s


def _delta(now_: float, prev: float) -> str:
    if prev <= 0:
        return "—" if now_ <= 0 else "🆕"
    pct = round((now_ - prev) / prev * 100)
    if pct > 0:
        return f"▲ {pct}%"
    if pct < 0:
        return f"▼ {abs(pct)}%"
    return "بدون تغییر"


def render_period_stats(s: PeriodStats) -> str:
    lines = [
        f"📊 آمار دورهٔ {s.label}",
        "",
        f"• مبلغِ کلِ فاکتورها: {_toman(s.billed)} تومان  ({_delta(s.billed, s.prev_billed)} نسبت به ماه قبل)",
        f"• وصول‌شده: {_toman(s.collected)} تومان  (نرخِ وصول: {s.collection_rate:g}٪)",
        f"• معوقِ این دوره: {_toman(s.outstanding)} تومان",
        f"• فاکتورها: {s.invoices} (پرداخت‌شده: {s.paid}) | بدهکار: {s.debtors}",
        f"• سرویس‌های فاکتورشده: {s.services}",
        "",
        f"💰 کلِ بدهیِ معوق (همهٔ دوره‌ها): {_toman(s.total_outstanding)} تومان",
    ]
    return "\n".join(lines)


# ----------------------------- per-panel breakdown -----------------------------
@dataclass
class PanelLine:
    key: str
    sales: float
    invoices: int
    status: str
    last_synced_at: dt.datetime | None
    stale: bool


async def per_panel(session: AsyncSession, label: str) -> list[PanelLine]:
    panels = (await session.execute(select(Panel).order_by(Panel.key))).scalars().all()
    sales_rows: dict[int, float] = {
        pid: float(total or 0)
        for pid, total in (
            await session.execute(
                select(Invoice.panel_id, func.coalesce(func.sum(Invoice.amount_toman), 0))
                .where(Invoice.period_label == label, Invoice.status.in_(_DELIVERED))
                .group_by(Invoice.panel_id)
            )
        ).all()
    }
    count_rows: dict[int, int] = {
        pid: int(n or 0)
        for pid, n in (
            await session.execute(
                select(Invoice.panel_id, func.count(Invoice.id))
                .where(Invoice.period_label == label, Invoice.status.in_(_DELIVERED))
                .group_by(Invoice.panel_id)
            )
        ).all()
    }
    now = dt.datetime.now(dt.timezone.utc)
    out: list[PanelLine] = []
    for p in panels:
        synced = p.last_synced_at
        if synced is not None and synced.tzinfo is None:
            synced = synced.replace(tzinfo=dt.timezone.utc)
        stale = synced is None or (now - synced) > dt.timedelta(hours=12)
        out.append(
            PanelLine(
                key=p.key,
                sales=float(sales_rows.get(p.id, 0) or 0),
                invoices=int(count_rows.get(p.id, 0) or 0),
                status=p.status.value,
                last_synced_at=synced,
                stale=stale,
            )
        )
    return out


def render_per_panel(label: str, rows: list[PanelLine]) -> str:
    if not rows:
        return "هیچ پنلی ثبت نشده است."
    lines = [f"🖥 تفکیکِ پنل — دورهٔ {label}", ""]
    for r in rows:
        health = "✅" if (r.status == "ok" and not r.stale) else ("🟠" if r.status == "ok" else "🔴")
        sync = "—"
        if r.last_synced_at is not None:
            sync = r.last_synced_at.astimezone(_TZ).strftime("%m-%d %H:%M") if _TZ else \
                r.last_synced_at.strftime("%m-%d %H:%M")
        stale_note = " (کهنه)" if r.stale else ""
        lines.append(_rtl(f"{health} {r.key}: {_toman(r.sales)} ت · {r.invoices} فاکتور · سینک {sync}{stale_note}"))
    return "\n".join(lines)


# ----------------------------- top debtors -----------------------------
@dataclass
class DebtorLine:
    reseller_id: int
    name: str
    total: float
    chat_id: int | None
    username: str | None


async def top_debtors(session: AsyncSession, limit: int = 10) -> list[DebtorLine]:
    rows = (
        await session.execute(
            select(
                Reseller.id, Reseller.name, func.sum(Invoice.amount_toman), Reseller.bot_chat_id
            )
            .join(Reseller, Invoice.reseller_id == Reseller.id)
            .where(Invoice.status.in_(_OWED))
            .group_by(Reseller.id, Reseller.name, Reseller.bot_chat_id)
            .order_by(func.sum(Invoice.amount_toman).desc())
            .limit(limit)
        )
    ).all()
    return [
        DebtorLine(reseller_id=rid, name=name or "—", total=float(total or 0),
                   chat_id=chat_id, username=None)
        for rid, name, total, chat_id in rows
    ]


# ----------------------------- system health -----------------------------
@dataclass
class Health:
    panels_total: int = 0
    panels_ok: int = 0
    problems: list[str] = field(default_factory=list)   # "key: reason"
    enforce_pending: int = 0
    enforce_failed: int = 0
    pending_payments: int = 0
    last_backup: str | None = None


async def health(session: AsyncSession) -> Health:
    h = Health()
    panels = (await session.execute(select(Panel))).scalars().all()
    h.panels_total = len(panels)
    now = dt.datetime.now(dt.timezone.utc)
    for p in panels:
        synced = p.last_synced_at
        if synced is not None and synced.tzinfo is None:
            synced = synced.replace(tzinfo=dt.timezone.utc)
        if p.status == PanelStatus.error:
            h.problems.append(f"{p.key}: همگام‌سازی ناموفق")
        elif synced is None:
            h.problems.append(f"{p.key}: هرگز همگام نشده")
        elif (now - synced) > dt.timedelta(hours=12):
            h.problems.append(f"{p.key}: سینکِ کهنه")
        else:
            h.panels_ok += 1
    h.enforce_pending = int(
        (
            await session.execute(
                select(func.count(EnforcementAction.id)).where(
                    EnforcementAction.dry_run.is_(False),
                    EnforcementAction.status.in_(_LIVE_ACTIONS),
                )
            )
        ).scalar_one()
        or 0
    )
    h.enforce_failed = int(
        (
            await session.execute(
                select(func.count(EnforcementAction.id)).where(
                    EnforcementAction.dry_run.is_(False),
                    EnforcementAction.status == EnforcementActionStatus.failed,
                )
            )
        ).scalar_one()
        or 0
    )
    h.pending_payments = int(
        (
            await session.execute(
                select(func.count(Payment.id)).where(Payment.status == PaymentStatus.pending)
            )
        ).scalar_one()
        or 0
    )
    # Source of truth: the timestamp stamped on every SUCCESSFUL backup (the auto-backup streams
    # to Telegram and never writes a zip to disk, so the disk folder can't be trusted). Fall back
    # to any on-disk zip only for installs that haven't produced a stamped backup yet.
    stamp = str(await settings_service.get(session, "last_backup_at", "") or "")
    h.last_backup = _format_backup_stamp(stamp) or _latest_backup_label()
    return h


def _format_backup_stamp(stamp: str) -> str | None:
    """Format an ISO last_backup_at timestamp as Tehran-local «YYYY-MM-DD HH:MM» (None if unset
    or unparseable → render_health shows «—»)."""
    if not stamp:
        return None
    try:
        ts = dt.datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.timezone.utc)
    local = ts.astimezone(_TZ) if _TZ else ts
    return local.strftime("%Y-%m-%d %H:%M")


def _latest_backup_label() -> str | None:
    try:
        backups = sorted(Path("data/backups").glob("invoice-backup-*.zip"))
        if not backups:
            return None
        latest = backups[-1]
        mtime = dt.datetime.fromtimestamp(latest.stat().st_mtime, tz=dt.timezone.utc)
        local = mtime.astimezone(_TZ) if _TZ else mtime
        return local.strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return None


def render_health(h: Health) -> str:
    lines = ["🩺 سلامتِ سامانه", ""]
    pmark = "✅" if h.panels_ok == h.panels_total else "🟠"
    lines.append(f"{pmark} پنل‌ها: {h.panels_ok}/{h.panels_total} سالم")
    for prob in h.problems:
        lines.append(_rtl(f"   🔴 {prob}"))
    lines.append(f"⛔️ صفِ مسدودسازی/بازگردانی در جریان: {h.enforce_pending}")
    if h.enforce_failed:
        lines.append(f"❌ اکشن‌های ناموفق (نیازمندِ بررسی): {h.enforce_failed}")
    if h.pending_payments:
        lines.append(f"💳 پرداخت‌های در انتظارِ تأیید: {h.pending_payments}")
    lines.append(f"🗄 آخرین پشتیبان: {h.last_backup or '—'}")
    return "\n".join(lines)


# ----------------------------- daily digest -----------------------------
async def daily_digest(session: AsyncSession) -> str:
    """A concise once-a-day summary for the owner's PV: this-month KPIs, today's confirmed
    payments, total debt, and any health warnings."""
    label = current_month().label
    s = await period_stats(session, label)
    since = _today_start_utc()
    paid_today_rows = (
        await session.execute(
            select(func.count(Payment.id), func.coalesce(func.sum(Payment.amount_toman), 0))
            .where(Payment.status == PaymentStatus.confirmed, Payment.verified_at >= since)
        )
    ).one()
    paid_today_n = int(paid_today_rows[0] or 0)
    paid_today_sum = float(paid_today_rows[1] or 0)
    h = await health(session)

    today_label = (tehran_today()).strftime("%Y-%m-%d")
    lines = [
        f"📰 خلاصهٔ روزانه — {today_label}",
        "",
        f"• فروشِ ماهِ جاری ({label}): {_toman(s.billed)} ت | وصول: {s.collection_rate:g}٪",
        f"• پرداخت‌های تأییدشدهٔ امروز: {paid_today_n} مورد ({_toman(paid_today_sum)} ت)",
        f"• بدهیِ کل: {_toman(s.total_outstanding)} ت | بدهکاران: {s.debtors}",
    ]
    warnings = list(h.problems)
    if h.enforce_failed:
        warnings.append(f"{h.enforce_failed} اکشنِ مسدودسازیِ ناموفق")
    if h.pending_payments:
        warnings.append(f"{h.pending_payments} پرداختِ در انتظارِ تأیید")
    if warnings:
        lines.append("")
        lines.append("⚠️ نیازمندِ توجه:")
        lines += [_rtl(f"   • {w}") for w in warnings]
    else:
        lines.append("")
        lines.append("✅ همه‌چیز سالم است.")
    return "\n".join(lines)


async def digest_enabled(session: AsyncSession) -> bool:
    return bool(await settings_service.get(session, "daily_digest_enabled", True))


def current_period_label() -> str:
    return current_month().label


def period_choices() -> list[tuple[str, str]]:
    """(label, caption) for the bot's period switch: this month, last month, 2 months back."""
    p: Period = current_month()
    out: list[tuple[str, str]] = []
    y, m = p.start.year, p.start.month
    for i in range(3):
        out.append((f"{y:04d}-{m:02d}", "این ماه" if i == 0 else ("ماه قبل" if i == 1 else f"{y:04d}-{m:02d}")))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return out
