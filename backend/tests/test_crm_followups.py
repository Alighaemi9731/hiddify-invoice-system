"""The anti-duplicate mechanism: logging a follow-up, snoozing, muting, and the three views.

The owner's actual complaint was re-contacting the same people. So the load-bearing assertion
here is that a logged follow-up removes the reseller from the default («سررسید پیگیری») view
IMMEDIATELY — not after the metric cache's TTL — and that the history row survives regardless.
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/crmfollow.db")
os.environ.setdefault("SECRET_KEY", "k")

import pytest  # noqa: E402
from fastapi import HTTPException, Response  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.api import crm as crm_api  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import (  # noqa: E402
    EndUserSnapshot,
    Panel,
    Reseller,
    ResellerCrmState,
    ResellerFollowup,
)
from app.models.enums import PanelStatus  # noqa: E402
from app.schemas.crm import BulkFollowupBody, FollowupBody  # noqa: E402
from app.services import crm  # noqa: E402

NOW = dt.datetime(2026, 8, 20, 9, 0, tzinfo=dt.timezone.utc)
LONG_AGO = NOW - dt.timedelta(days=400)
ACTOR = "owner"


def _run(body, tmp_path):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'crmf.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as s:
                crm.invalidate_metrics_cache()
                await body(s)
        finally:
            crm.invalidate_metrics_cache()
            await engine.dispose()
    asyncio.run(go())


async def _seed(s, count=3):
    p = Panel(key="p1", host="p1.invalid", proxy_path_enc="x", owner_uuid="owner",
              status=PanelStatus.ok, last_synced_at=NOW, enabled=True)
    s.add(p)
    await s.flush()
    s.add(Reseller(panel_id=p.id, admin_uuid="owner", name="Owner", is_owner=True,
                   last_seen_at=NOW, created_at=LONG_AGO))
    made = []
    for i in range(count):
        # Linked to the bot: an unlinked reseller lands in «وصل‌نشده به ربات», which outranks
        # the churn buckets these queue tests are written against.
        r = Reseller(panel_id=p.id, admin_uuid=f"r{i}", name=f"R{i}", last_seen_at=NOW,
                     created_at=LONG_AGO, bot_chat_id=50_000 + i)
        s.add(r)
        made.append(r)
    await s.flush()
    # Everyone sold something long ago → all dormant/churned, i.e. all on the queue.
    for i, r in enumerate(made):
        s.add(EndUserSnapshot(panel_id=p.id, user_uuid=f"u{i}", added_by_uuid=r.admin_uuid,
                              usage_limit_gb=50, start_date=dt.date(2026, 5, 1),
                              last_synced_at=NOW))
    await s.commit()
    return p, made


async def _board(s, **kw):
    """Call the endpoint directly — so every Query()-defaulted param must be passed
    explicitly, since FastAPI's dependency resolution is not running here."""
    response = kw.pop("response", Response())
    params = dict(segment=None, panel_id=None, q=None, view="due", sort="value",
                  order="desc", limit=100, offset=0)
    params.update(kw)
    return await crm_api.board(response=response, session=s, **params)


async def _names(s, **kw):
    return [r.reseller_name for r in await _board(s, **kw)]


# ---------------------------------------------------------------- the core loop
def test_logging_a_followup_drops_the_row_off_the_queue_immediately(tmp_path):
    """Metrics are cached for 5 minutes; follow-up state must NOT be, or the owner would keep
    seeing someone they just contacted."""
    async def body(s):
        _p, made = await _seed(s)
        assert await _names(s) == ["R0", "R1", "R2"]

        await crm_api.log_followup(
            made[1].id, FollowupBody(note="زنگ زدم، گفت هفتهٔ بعد"), session=s, actor=ACTOR
        )
        assert await _names(s) == ["R0", "R2"]                 # gone from the queue at once
        assert await _names(s, view="all") == ["R0", "R1", "R2"]
        assert await _names(s, view="snoozed") == ["R1"]
    _run(body, tmp_path)


def test_the_default_snooze_comes_from_settings_and_expires_by_date(tmp_path):
    async def body(s):
        _p, made = await _seed(s, count=1)
        result = await crm_api.log_followup(
            made[0].id, FollowupBody(), session=s, actor=ACTOR
        )
        expected = crm.today() + dt.timedelta(days=crm.Thresholds().snooze_default_days)
        assert result.snoozed_until == expected

        state = (await s.execute(select(ResellerCrmState))).scalar_one()
        # Expired snooze → back on the queue, no action needed.
        state.snoozed_until = crm.today() - dt.timedelta(days=1)
        await s.commit()
        assert await _names(s) == ["R0"]
    _run(body, tmp_path)


def test_an_explicit_zero_day_snooze_keeps_the_row_on_the_queue(tmp_path):
    """"I messaged them, but keep them in front of me" — distinct from omitting the field,
    which applies the owner's default."""
    async def body(s):
        _p, made = await _seed(s, count=1)
        result = await crm_api.log_followup(
            made[0].id, FollowupBody(snooze_days=0), session=s, actor=ACTOR
        )
        assert result.snoozed_until is None
        assert await _names(s) == ["R0"]
        assert (await s.execute(select(ResellerCrmState))).scalar_one().touch_count == 1
    _run(body, tmp_path)


def test_mute_outranks_any_date_and_survives_expiry(tmp_path):
    async def body(s):
        _p, made = await _seed(s, count=1)
        await crm_api.log_followup(
            made[0].id, FollowupBody(muted=True, snooze_days=90), session=s, actor=ACTOR
        )
        state = (await s.execute(select(ResellerCrmState))).scalar_one()
        assert state.muted is True and state.snoozed_until is None
        assert await _names(s) == []
        assert await _names(s, view="snoozed") == ["R0"]
    _run(body, tmp_path)


def test_clearing_a_snooze_returns_the_row_without_counting_as_outreach(tmp_path):
    async def body(s):
        _p, made = await _seed(s, count=1)
        await crm_api.log_followup(made[0].id, FollowupBody(muted=True), session=s, actor=ACTOR)
        assert await _names(s) == []

        await crm_api.clear_snooze(made[0].id, session=s)
        assert await _names(s) == ["R0"]
        state = (await s.execute(select(ResellerCrmState))).scalar_one()
        assert state.touch_count == 1                       # unchanged — undo is not a touch
        # Idempotent for a reseller that was never touched.
        assert (await crm_api.clear_snooze(9999, session=s)).updated == 0
    _run(body, tmp_path)


# ---------------------------------------------------------------- history
def test_the_log_records_the_segment_at_the_time_of_the_touch(tmp_path):
    """The board recomputes segments live, so without freezing it here "why did I contact this
    person?" would be unanswerable a month later."""
    async def body(s):
        _p, made = await _seed(s, count=1)
        await crm_api.log_followup(
            made[0].id, FollowupBody(note="پیگیری اول"), session=s, actor=ACTOR
        )
        row = (await s.execute(select(ResellerFollowup))).scalar_one()
        assert row.segment == "churned"                     # last sale was 2026-05
        assert row.note == "پیگیری اول" and row.actor == ACTOR
        # Denormalized identity, so the history stays readable after the reseller is deleted.
        assert row.reseller_name == "R0" and row.panel_key == "p1"
        assert row.reseller_admin_uuid == "r0"
    _run(body, tmp_path)


def test_repeated_touches_accumulate_and_the_pinned_note_is_separate(tmp_path):
    async def body(s):
        _p, made = await _seed(s, count=1)
        await crm_api.log_followup(
            made[0].id, FollowupBody(note="بار اول", pinned_note="مشتری قدیمی"),
            session=s, actor=ACTOR,
        )
        await crm_api.log_followup(
            made[0].id, FollowupBody(note="بار دوم"), session=s, actor=ACTOR
        )
        state = (await s.execute(select(ResellerCrmState))).scalar_one()
        assert state.touch_count == 2
        # Omitting pinned_note leaves it alone rather than blanking it.
        assert state.note == "مشتری قدیمی"

        log = await crm_api.followup_log(response=Response(), session=s, reseller_id=None,
                                         q=None, limit=100, offset=0)
        assert [r.note for r in log] == ["بار دوم", "بار اول"]     # newest first
    _run(body, tmp_path)


def test_bulk_followup_touches_every_selected_reseller_once(tmp_path):
    async def body(s):
        _p, made = await _seed(s)
        result = await crm_api.log_followups_bulk(
            BulkFollowupBody(reseller_ids=[made[0].id, made[2].id], note="پیام گروهی"),
            session=s, actor=ACTOR,
        )
        assert result.updated == 2
        assert await _names(s) == ["R1"]
        rows = (await s.execute(select(ResellerFollowup))).scalars().all()
        assert {r.reseller_name for r in rows} == {"R0", "R2"}
    _run(body, tmp_path)


def test_a_followup_for_an_unknown_reseller_is_rejected(tmp_path):
    async def body(s):
        await _seed(s, count=1)
        with pytest.raises(HTTPException) as e:
            await crm_api.log_followup(4242, FollowupBody(), session=s, actor=ACTOR)
        assert e.value.status_code == 404
    _run(body, tmp_path)


# ---------------------------------------------------------------- board plumbing
def test_summary_counts_the_whole_population_not_the_page(tmp_path):
    async def body(s):
        _p, made = await _seed(s)
        await crm_api.log_followup(made[0].id, FollowupBody(), session=s, actor=ACTOR)
        await crm_api.log_followup(made[1].id, FollowupBody(muted=True), session=s, actor=ACTOR)

        summary = await crm_api.summary(session=s)
        assert summary.total == 3
        assert summary.counts["churned"] == 3               # segment is independent of snoozing
        assert set(summary.counts) == set(crm.SEGMENTS)
        assert summary.due == 1 and summary.snoozed == 1 and summary.muted == 1
    _run(body, tmp_path)


def test_board_paginates_and_reports_the_filtered_total(tmp_path):
    async def body(s):
        await _seed(s, count=5)
        response = Response()
        rows = await _board(s, response=response, sort="name", order="asc", limit=2, offset=2)
        assert [r.reseller_name for r in rows] == ["R2", "R3"]
        assert response.headers["X-Total-Count"] == "5"     # the whole filtered set, not the page
    _run(body, tmp_path)


def test_board_filters_by_segment_panel_and_search(tmp_path):
    async def body(s):
        p, _made = await _seed(s, count=3)
        assert await _names(s, q="R1") == ["R1"]
        assert await _names(s, q="r1") == ["R1"]                     # case-insensitive
        assert await _names(s, segment="churned", sort="name", order="asc") == ["R0", "R1", "R2"]
        assert await _names(s, segment="healthy") == []
        assert await _names(s, panel_id=p.id + 99) == []
    _run(body, tmp_path)


def test_reseller_detail_returns_the_full_history_and_the_chart_series(tmp_path):
    async def body(s):
        _p, made = await _seed(s, count=1)
        await crm_api.log_followup(made[0].id, FollowupBody(note="یادداشت"), session=s,
                                   actor=ACTOR)
        detail = await crm_api.reseller_detail(made[0].id, session=s)
        assert detail.row.reseller_name == "R0" and detail.row.due is False
        assert len(detail.months) == crm.HISTORY_MONTHS
        assert [f.note for f in detail.followups] == ["یادداشت"]

        with pytest.raises(HTTPException) as e:
            await crm_api.reseller_detail(4242, session=s)
        assert e.value.status_code == 404
    _run(body, tmp_path)
