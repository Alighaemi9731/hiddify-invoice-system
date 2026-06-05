"""
Abuse-resistant usage metering (billing model "C").

Normal billing (quota of users whose service was created in the month) is unchanged.
On top of it we ADD the abuse that the snapshot rule misses, measured from the
periodic sync's deltas:

  • overage_gb      — true consumption beyond the paid-for buffer. Catches the
    "create a small package then reset usage daily so it never ends" trick: we track
    cumulative real usage (reset-aware) and bill anything past what was provisioned.
  • edit_renewal_gb — quota topped up WITHOUT updating start_date. Catches "renew an
    expired user with the Edit button instead of the renew button", which keeps the
    old start_date so the snapshot rule never re-bills it.

`apply()` runs inside each sync (no DB I/O — pure state update). `bundle_extra()`
sums the abnormal GB for a reseller bundle at billing time. `notify_abuse_if_any()`
tells the reseller (and the owner) exactly what was detected and that it's billed.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import UsageMeter
from app.models.enums import DeliveryKind
from app.services import settings_service

log = logging.getLogger("metering")

_EPS = 0.05  # ignore sub-50MB noise


def period_of(d) -> str:
    return d.strftime("%Y-%m") if d else ""


async def is_enabled(session: AsyncSession) -> bool:
    return bool(await settings_service.get(session, "metering_enabled", True))


async def load_period_meters(
    session: AsyncSession, panel_id: int, period_label: str
) -> dict[str, UsageMeter]:
    rows = (
        await session.execute(
            select(UsageMeter).where(
                UsageMeter.panel_id == panel_id, UsageMeter.period_label == period_label
            )
        )
    ).scalars().all()
    return {m.user_uuid: m for m in rows}


def apply(
    *,
    snapshot,                 # EndUserSnapshot (running state lives here)
    meter: UsageMeter,        # this month's bucket
    prev_limit: float,
    prev_used: float,
    new_limit: float,
    new_used: float,
    start_date,
    added_by_uuid: str | None,
    name: str | None,
    period_label: str,
) -> None:
    """Update the per-user running state (on `snapshot`) and the monthly `meter`.
    Pure (no DB); call BEFORE overwriting the snapshot's usage fields."""
    meter.added_by_uuid = added_by_uuid
    meter.name = (name or "")[:255]
    in_period = period_of(start_date) == period_label

    # First time we see this user (a brand-new user, OR an existing one when metering
    # is first switched on). Set the baseline; only bill it if genuinely created now.
    if not snapshot.meter_init:
        snapshot.meter_provisioned_gb = new_limit
        snapshot.meter_consumed_gb = new_used
        snapshot.meter_init = True
        if in_period:
            meter.quota_added_gb = float(meter.quota_added_gb or 0) + new_limit
        return

    prev_start = snapshot.start_date  # not yet overwritten by the caller

    # LEGITIMATE renewal: the renew button advances start_date (and usually resets
    # current_usage). That's a fresh cycle with a fresh quota — re-baseline so the
    # PREVIOUS cycle's consumption can't show up as "overage" against the new quota.
    # (The new package is billed by the normal start_date rule, not here.)
    if start_date is not None and (prev_start is None or start_date > prev_start):
        snapshot.meter_provisioned_gb = new_limit
        snapshot.meter_consumed_gb = new_used
        if in_period:
            meter.quota_added_gb = float(meter.quota_added_gb or 0) + new_limit
        return

    # Quota increase = new sale / top-up / renewal-by-edit (start_date unchanged).
    add = new_limit - prev_limit
    if add > _EPS:
        meter.quota_added_gb = float(meter.quota_added_gb or 0) + add
        snapshot.meter_provisioned_gb = float(snapshot.meter_provisioned_gb or 0) + add
        if not in_period:
            # Topped up without a fresh start_date → renew-by-edit (snapshot rule misses it).
            meter.edit_renewal_gb = float(meter.edit_renewal_gb or 0) + add

    # Consumption since last sync (reset-aware).
    if new_used + _EPS >= prev_used:
        dc = new_used - prev_used
    else:
        dc = new_used                      # current_usage dropped → a reset happened
        meter.reset_count = int(meter.reset_count or 0) + 1
    if dc < 0:
        dc = 0.0

    buffer = float(snapshot.meter_provisioned_gb or 0) - float(snapshot.meter_consumed_gb or 0)
    if buffer < 0:
        buffer = 0.0
    overage = dc - buffer
    if overage < 0:
        overage = 0.0

    meter.consumed_gb = float(meter.consumed_gb or 0) + dc
    snapshot.meter_consumed_gb = float(snapshot.meter_consumed_gb or 0) + dc
    if overage > _EPS:
        meter.overage_gb = float(meter.overage_gb or 0) + overage


async def bundle_extra(
    session: AsyncSession,
    panel_id: int,
    admin_uuids: set[str],
    period_label: str,
    free_threshold_gb: float,
) -> dict:
    """The ABNORMAL extra GB to add to a bundle's invoice for the period, plus a
    per-user breakdown for the notification. Returns {gb, lines, abnormal}."""
    if not admin_uuids or not await is_enabled(session):
        return {"gb": 0.0, "lines": [], "abnormal": []}
    rows = (
        await session.execute(
            select(UsageMeter).where(
                UsageMeter.panel_id == panel_id,
                UsageMeter.period_label == period_label,
                UsageMeter.added_by_uuid.in_(admin_uuids),
            )
        )
    ).scalars().all()

    total = 0.0
    lines: list[dict] = []
    abnormal: list[dict] = []
    for m in rows:
        over = float(m.overage_gb or 0)
        edit = float(m.edit_renewal_gb or 0)
        extra = 0.0
        if over > _EPS:
            extra += over
        if edit > free_threshold_gb:        # ignore tiny test-config renewals
            extra += edit
        if extra <= _EPS:
            continue
        total += extra
        lines.append({
            "user_uuid": m.user_uuid, "name": m.name or "",
            "usage_gb": round(extra, 3), "added_by_uuid": m.added_by_uuid,
        })
        abnormal.append({
            "name": m.name or m.user_uuid[-6:], "user_uuid": m.user_uuid,
            "overage_gb": round(over, 3), "edit_renewal_gb": round(edit, 3),
            "reset_count": int(m.reset_count or 0), "billed_gb": round(extra, 3),
        })
    return {"gb": round(total, 3), "lines": lines, "abnormal": abnormal}


def _abuse_text(period: str, abnormal: list[dict], price_per_gb: int) -> str:
    lines = [
        "⚠️ توجه: در فاکتور دوره " + period + " مواردی شناسایی شد که طبق روال عادی محاسبه "
        "نمی‌شدند ولی رصد شده و به فاکتور شما اضافه شده‌اند:\n",
    ]
    for a in abnormal:
        parts = []
        if a["overage_gb"] > 0:
            parts.append(
                f"مصرف بیش از حجم فروخته‌شده (احتمال ریست مصرف): {a['overage_gb']:g} گیگ"
                + (f" — {a['reset_count']} بار ریست" if a["reset_count"] else "")
            )
        if a["edit_renewal_gb"] > 0:
            parts.append(
                f"تمدید/شارژ با ویرایش بدون به‌روزرسانی تاریخ شروع: {a['edit_renewal_gb']:g} گیگ"
            )
        lines.append(f"• کاربر «{a['name']}» → " + "؛ ".join(parts) + f"  (محاسبه‌شده: {a['billed_gb']:g} گیگ)")
    lines.append(
        "\nلطفاً برای تمدید از دکمهٔ تمدید پنل استفاده کنید (نه ویرایش حجم/تاریخ) و مصرف "
        "کاربران را ریست نکنید. این موارد به‌صورت خودکار رصد و در فاکتور لحاظ می‌شوند."
    )
    return "\n".join(lines)


async def notify_abuse_if_any(session: AsyncSession, invoice, reseller, *, bot=None) -> None:
    """If this invoice's bundle has abnormal (metered-extra) usage, message the reseller
    with a full breakdown and ping the owner. Best-effort; never raises into delivery."""
    try:
        if not await is_enabled(session):
            return
        from app.services import notifier, owner_notify, pricing
        from app.services.reseller_report import node_descendants

        descendants = await node_descendants(session, reseller)
        uuids = {d.admin_uuid for d in descendants}
        free_threshold = await pricing.get_free_threshold_gb(session)
        extra = await bundle_extra(session, invoice.panel_id, uuids, invoice.period_label, free_threshold)
        if not extra["abnormal"]:
            return

        price = int(reseller.price_per_gb or await pricing.get_default_price_per_gb(session))
        text = _abuse_text(invoice.period_label, extra["abnormal"], price)
        await notifier.send_to_reseller(
            session, reseller, text, kind=DeliveryKind.abuse_notice, invoice_id=invoice.id, bot=bot
        )
        # Owner heads-up (clickable reseller link).
        total_extra = extra["gb"]
        await owner_notify.notify_owner(
            session,
            f"🚨 مصرف غیرعادی شناسایی شد — نماینده {owner_notify.user_link(reseller)} "
            f"(پنل #{invoice.panel_id})، دوره {invoice.period_label}: "
            f"{len(extra['abnormal'])} کاربر، مجموع {total_extra:g} گیگ اضافه به فاکتور.",
            html=True,
        )
    except Exception:  # noqa: BLE001
        log.warning("notify_abuse_if_any failed for invoice %s", getattr(invoice, "id", "?"),
                    exc_info=True)
