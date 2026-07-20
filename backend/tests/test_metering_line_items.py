"""Typed, enumerated metering line items (v1.99.0).

The merged « — مصرف اضافه/تمدید» blob is gone: every metered extra renders as its OWN
typed line — renewals enumerated per event («تمدید اول»، «تمدید دوم», from the new
`usage_meter_events` log), edit top-ups and overage each named — while the MONEY stays
byte-identical to the old single line (events refine presentation only; a month whose
events don't reconcile with the aggregate falls back to one typed aggregate line).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/mli.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.core import crypto  # noqa: E402
from app.models import EndUserSnapshot, Panel, UsageMeterEvent  # noqa: E402
from app.services import sync as sync_service  # noqa: E402
from app.services.metering import (  # noqa: E402
    LABEL_EDIT,
    LABEL_OVERAGE,
    LABEL_RENEWAL,
    _extra_from_rows,
    compute,
)
from app.services.panel_client.base import (  # noqa: E402
    PanelAdmin,
    PanelData,
    PanelUser,
)

_OWNER = "owner-uuid"

# Month-independent dates: everything anchors on the CURRENT month because the sync
# meters against `now`'s period label.
_TODAY = dt.date.today()
_D1 = _TODAY.replace(day=1)
_D2 = _TODAY.replace(day=2)
_D3 = _TODAY.replace(day=3)
_PERIOD = _TODAY.strftime("%Y-%m")


def _snap(**kw):
    d = dict(meter_init=True, meter_provisioned_gb=0.0, meter_consumed_gb=0.0, start_date=None)
    d.update(kw)
    return SimpleNamespace(**d)


def _meterns(**kw):
    d = dict(added_by_uuid=None, name="", quota_added_gb=0.0, edit_renewal_gb=0.0,
             consumed_gb=0.0, overage_gb=0.0, renew_used_gb=0.0, reset_count=0)
    d.update(kw)
    return SimpleNamespace(**d)


# ───────────────────────── compute() emits events ─────────────────────────

def test_compute_emits_renewal_event():
    snap = _snap(meter_provisioned_gb=30.0, meter_consumed_gb=28.5, start_date=_D1)
    u = compute(snapshot=snap, meter=_meterns(), prev_limit=30, prev_used=28.5,
                new_limit=30, new_used=0.0, start_date=_D2, added_by_uuid="A", name="u",
                period_label=_PERIOD)
    assert u.events == (("renewal", 28.5),)
    assert u.renew_used_gb == 28.5


def test_compute_cumulative_renewal_emits_no_event():
    snap = _snap(meter_provisioned_gb=30.0, meter_consumed_gb=28.0, start_date=_D1)
    u = compute(snapshot=snap, meter=_meterns(), prev_limit=30, prev_used=28.0,
                new_limit=58, new_used=28.0, start_date=_D2, added_by_uuid="A", name="u",
                period_label=_PERIOD)
    assert u.events == ()
    assert u.renew_used_gb == 0.0


def test_compute_emits_edit_topup_event():
    prev_month_start = (_D1 - dt.timedelta(days=1)).replace(day=1)
    snap = _snap(meter_provisioned_gb=10.0, meter_consumed_gb=9.0, start_date=prev_month_start)
    u = compute(snapshot=snap, meter=_meterns(), prev_limit=10, prev_used=9.0,
                new_limit=25, new_used=9.0, start_date=prev_month_start,
                added_by_uuid="A", name="u", period_label=_PERIOD)
    assert u.events == (("edit_topup", 15.0),)
    assert u.edit_renewal_gb == 15.0


def test_compute_init_emits_no_event():
    u = compute(snapshot=_snap(meter_init=False), meter=_meterns(), prev_limit=0, prev_used=0,
                new_limit=30, new_used=0, start_date=_D1, added_by_uuid="A", name="u",
                period_label=_PERIOD)
    assert u.events == ()


# ───────────────────────── typed line splitting ─────────────────────────

def _mrow(**kw):
    d = dict(user_uuid="x1", name="گوشی", added_by_uuid="A", overage_gb=0.0,
             edit_renewal_gb=0.0, renew_used_gb=0.0, reset_count=0)
    d.update(kw)
    return SimpleNamespace(**d)


def test_two_renewals_enumerate_with_events():
    rows = [_mrow(renew_used_gb=57.5)]
    events = {"x1": [("renewal", 28.5), ("renewal", 29.0)]}
    out = _extra_from_rows(rows, 1.0, 0.5, None, events_by_user=events)
    names = [ln["display_name"] for ln in out["lines"]]
    assert names == [f"گوشی{LABEL_RENEWAL} اول", f"گوشی{LABEL_RENEWAL} دوم"]
    assert [ln["usage_gb"] for ln in out["lines"]] == [28.5, 29.0]
    assert all(ln["kind"] == "renewal" for ln in out["lines"])
    assert out["gb"] == pytest.approx(57.5)
    assert out["abnormal"] == []  # legit renewals are never flagged as abuse


def test_single_renewal_gets_plain_label():
    out = _extra_from_rows([_mrow(renew_used_gb=28.5)], 1.0, 0.5, None,
                           events_by_user={"x1": [("renewal", 28.5)]})
    assert [ln["display_name"] for ln in out["lines"]] == [f"گوشی{LABEL_RENEWAL}"]


def test_legacy_month_without_events_falls_back_to_one_typed_line():
    out = _extra_from_rows([_mrow(renew_used_gb=57.5)], 1.0, 0.5, None, events_by_user=None)
    assert [ln["display_name"] for ln in out["lines"]] == [f"گوشی{LABEL_RENEWAL}"]
    assert out["lines"][0]["usage_gb"] == pytest.approx(57.5)
    assert out["gb"] == pytest.approx(57.5)


def test_mismatched_events_fall_back_to_aggregate():
    # Events lost/partial (e.g. logged only since mid-month) → never trust them for money.
    out = _extra_from_rows([_mrow(renew_used_gb=57.5)], 1.0, 0.5, None,
                           events_by_user={"x1": [("renewal", 28.5)]})
    assert [ln["display_name"] for ln in out["lines"]] == [f"گوشی{LABEL_RENEWAL}"]
    assert out["lines"][0]["usage_gb"] == pytest.approx(57.5)


def test_all_three_kinds_split_and_sum_exactly():
    rows = [_mrow(renew_used_gb=20.0, edit_renewal_gb=15.0, overage_gb=7.3)]
    events = {"x1": [("renewal", 20.0), ("edit_topup", 15.0)]}
    out = _extra_from_rows(rows, 1.0, 0.5, None, events_by_user=events)
    by_kind = {ln["kind"]: ln for ln in out["lines"]}
    assert set(by_kind) == {"renewal", "edit", "overage"}
    assert by_kind["renewal"]["display_name"].endswith(LABEL_RENEWAL)
    assert by_kind["edit"]["display_name"].endswith(LABEL_EDIT)
    assert by_kind["overage"]["display_name"].endswith(LABEL_OVERAGE)
    assert sum(ln["usage_gb"] for ln in out["lines"]) == pytest.approx(42.3)
    assert out["gb"] == pytest.approx(42.3)
    # overage + edit are abuse; renewal is not
    assert out["abnormal"][0]["billed_gb"] == pytest.approx(22.3)


def test_multiple_edit_topups_enumerate():
    rows = [_mrow(edit_renewal_gb=25.0)]
    events = {"x1": [("edit_topup", 10.0), ("edit_topup", 15.0)]}
    out = _extra_from_rows(rows, 1.0, 0.5, None, events_by_user=events)
    names = [ln["display_name"] for ln in out["lines"]]
    assert names == [f"گوشی{LABEL_EDIT} (نوبت اول)", f"گوشی{LABEL_EDIT} (نوبت دوم)"]
    assert sum(ln["usage_gb"] for ln in out["lines"]) == pytest.approx(25.0)


def test_money_identical_to_old_merged_line():
    # Same aggregates, with and without events → identical billed total.
    rows = [_mrow(renew_used_gb=20.0, edit_renewal_gb=15.0, overage_gb=7.3)]
    with_events = _extra_from_rows(
        rows, 1.0, 0.5, None,
        events_by_user={"x1": [("renewal", 20.0), ("edit_topup", 15.0)]})
    without = _extra_from_rows(rows, 1.0, 0.5, None)
    assert with_events["gb"] == without["gb"] == pytest.approx(42.3)
    assert (sum(ln["usage_gb"] for ln in with_events["lines"])
            == pytest.approx(sum(ln["usage_gb"] for ln in without["lines"])))


def test_thresholds_still_apply_to_split_lines():
    # Overage at/below tolerance and edit at/below the free threshold stay unbilled.
    out = _extra_from_rows([_mrow(overage_gb=0.4, edit_renewal_gb=0.8)], 1.0, 0.5, None,
                           events_by_user={"x1": [("edit_topup", 0.8)]})
    assert out["lines"] == []
    assert out["gb"] == 0.0


def test_stale_events_overshooting_aggregate_never_inflate_lines():
    # Events reconcile within the 0.06 tolerance but sum ABOVE the billed aggregate (a stale
    # pre-purge event survived) → the split must NOT bill the event numbers; the safe rebuild
    # keeps the lines summing exactly to the aggregate.
    rows = [_mrow(renew_used_gb=10.0)]
    events = {"x1": [("renewal", 4.0), ("renewal", 6.055)]}  # sum 10.055, drift 0.055 ≤ 0.06
    out = _extra_from_rows(rows, 1.0, 0.5, None, events_by_user=events)
    assert sum(ln["usage_gb"] for ln in out["lines"]) == pytest.approx(10.0)
    assert out["gb"] == pytest.approx(10.0)


def test_service_count_rule_is_shared_and_consistent():
    from app.services.pdf import service_count

    lines = [
        # two base services (one later deleted from the panel — still a sale)
        {"name": "a", "uuid": "u1", "start_date": _D1, "usage_gb": 10},
        {"name": "b — مصرف حذف‌شده از پنل", "uuid": "u2", "start_date": _D2, "usage_gb": 5},
        # one metered config split into three typed lines → counts ONCE
        {"name": f"c{LABEL_RENEWAL} اول", "uuid": "u3", "start_date": None, "usage_gb": 8},
        {"name": f"c{LABEL_RENEWAL} دوم", "uuid": "u3", "start_date": None, "usage_gb": 9},
        {"name": f"c{LABEL_OVERAGE}", "uuid": "u3", "start_date": None, "usage_gb": 2},
        # artifacts → never counted
        {"name": "🏪 هزینهٔ ماهانهٔ ربات فروشگاهی", "uuid": "storefront_fee_7",
         "start_date": None, "usage_gb": 0},
        {"name": "تعدیل فاکتور", "uuid": "", "start_date": None, "usage_gb": 1},
    ]
    assert service_count(lines) == 3  # 2 base + 1 distinct metered config

    # A user with BOTH a base line and extra lines counts twice (base + metered), exactly
    # matching Invoice.users_count = base_users_count + distinct extra users.
    both = [
        {"name": "d", "uuid": "u4", "start_date": _D1, "usage_gb": 30},
        {"name": f"d{LABEL_RENEWAL}", "uuid": "u4", "start_date": None, "usage_gb": 28},
    ]
    assert service_count(both) == 2


# ───────────────────────── sync writes the event log ─────────────────────────

def _pdata(users):  # noqa: ANN001
    return PanelData(
        admins=[PanelAdmin(uuid=_OWNER, name="Owner", parent_admin_uuid=None, mode="super_admin",
                           comment=None, telegram_id=None, max_users=100, max_active_users=100,
                           can_add_admin=True)],
        users=users,
    )


def _puser(uuid, *, limit, used, start):  # noqa: ANN001
    return PanelUser(uuid=uuid, name="گوشی", added_by_uuid=_OWNER, start_date=start,
                     usage_limit_gb=limit, current_usage_gb=used, package_days=30, enable=True,
                     is_active=True, mode="no_reset", last_online=None, comment=None)


def test_sync_persists_renewal_events_and_bundle_enumerates(tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'mli-sync.db'}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        S = async_sessionmaker(engine, expire_on_commit=False)
        try:
            from app.services import metering, settings_service
            async with S() as s:
                await settings_service.set_value(s, "metering_enabled", True)
                panel = Panel(key="pm", host="h", proxy_path_enc=crypto.encrypt("x") or "",
                              owner_uuid=_OWNER)
                s.add(panel)
                await s.flush()
                pid = panel.id
                await s.commit()

            # Cycle 1: created on day 1, sold 30, burns 28.5.
            async with S() as s:
                await sync_service.sync_panel(
                    s, await s.get(Panel, pid),
                    data=_pdata([_puser("u1", limit=30, used=0.0, start=_D1)]))
            async with S() as s:
                await sync_service.sync_panel(
                    s, await s.get(Panel, pid),
                    data=_pdata([_puser("u1", limit=30, used=28.5, start=_D1)]))
            # Renewal #1 (clean reset): fresh 30, usage back to 0, start advanced.
            async with S() as s:
                await sync_service.sync_panel(
                    s, await s.get(Panel, pid),
                    data=_pdata([_puser("u1", limit=30, used=0.0, start=_D2)]))
            # Cycle 2 burns 29.0, then renewal #2.
            async with S() as s:
                await sync_service.sync_panel(
                    s, await s.get(Panel, pid),
                    data=_pdata([_puser("u1", limit=30, used=29.0, start=_D2)]))
            async with S() as s:
                await sync_service.sync_panel(
                    s, await s.get(Panel, pid),
                    data=_pdata([_puser("u1", limit=30, used=0.0, start=_D3)]))

            async with S() as s:
                evs = (await s.execute(
                    select(UsageMeterEvent).order_by(UsageMeterEvent.id))).scalars().all()
                assert [(e.kind, float(e.gb)) for e in evs] == [
                    ("renewal", 28.5), ("renewal", 29.0)]
                assert {e.period_label for e in evs} == {_PERIOD}

                extra = await metering.bundle_extra(s, pid, {_OWNER}, _PERIOD, 1.0)
                names = [ln["display_name"] for ln in extra["lines"]]
                assert names == [f"گوشی{LABEL_RENEWAL} اول", f"گوشی{LABEL_RENEWAL} دوم"]
                assert [ln["usage_gb"] for ln in extra["lines"]] == [28.5, 29.0]
                assert extra["gb"] == pytest.approx(57.5)

                # The final snapshot state is a fresh 30 GB cycle (billed by the base rule).
                snap = (await s.execute(select(EndUserSnapshot).where(
                    EndUserSnapshot.user_uuid == "u1"))).scalar_one()
                assert snap.start_date == _D3
        finally:
            await engine.dispose()
    asyncio.run(go())


# ───────────────────────── invoice lines + users_count ─────────────────────────

def test_write_lines_and_users_count_with_split_extras(tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'mli-lines.db'}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        S = async_sessionmaker(engine, expire_on_commit=False)
        try:
            from app.models import Invoice, InvoiceLine, Reseller
            from app.models.enums import InvoiceStatus
            from app.services import invoicing, settings_service

            extra = {
                "gb": 57.5,
                "abnormal": [],
                "lines": [
                    {"user_uuid": "x1", "name": "گوشی",
                     "display_name": f"گوشی{LABEL_RENEWAL} اول",
                     "usage_gb": 28.5, "added_by_uuid": "A", "kind": "renewal"},
                    {"user_uuid": "x1", "name": "گوشی",
                     "display_name": f"گوشی{LABEL_RENEWAL} دوم",
                     "usage_gb": 29.0, "added_by_uuid": "A", "kind": "renewal"},
                ],
            }
            async with S() as s:
                await settings_service.seed_defaults(s)
                s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o"))
                r = Reseller(panel_id=1, admin_uuid="A", name="R")
                s.add(r)
                await s.flush()
                inv = Invoice(reseller_id=r.id, panel_id=1,
                              period_start=_D1, period_end=_TODAY, period_label=_PERIOD,
                              usage_gb=0, amount_toman=0, amount_usdt=0,
                              status=InvoiceStatus.draft)
                s.add(inv)
                await s.flush()

                invoicing._write_lines(
                    s, inv, base_lines=[], extra=extra, storefront_fee=0, reseller_id=r.id)
                await s.flush()
                rows = (await s.execute(select(InvoiceLine).where(
                    InvoiceLine.invoice_id == inv.id).order_by(InvoiceLine.id))).scalars().all()
                assert [ln.name for ln in rows] == [
                    f"گوشی{LABEL_RENEWAL} اول", f"گوشی{LABEL_RENEWAL} دوم"]
                assert sum(float(ln.usage_gb) for ln in rows) == pytest.approx(57.5)
                assert all(ln.start_date is None for ln in rows)
                assert all((ln.added_by_uuid or "") == "A" for ln in rows)

                # users_count counts DISTINCT metered users, not split lines.
                totals = await invoicing._compute_totals(
                    s, r, base_gb=0.0, base_users_count=3, extra=extra,
                    price=1000, bundle_min_sale=0, period_start=_D1)
                assert totals.users_count == 4  # 3 base + ONE metered user (not +2 lines)
                assert totals.total_gb == pytest.approx(57.5)
        finally:
            await engine.dispose()
    asyncio.run(go())
