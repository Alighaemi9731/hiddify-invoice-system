"""Prod incident 2026-07-18 — a suspension disabled 305 OTHER resellers' users.

Root cause: `EndUserSnapshot.panel_user_id` cached Hiddify's numeric user id durably and every
later enforcement reused it WITHOUT re-resolving. Hiddify renumbers its user table on a panel
restore/re-import, so those cached ids came to belong to completely different users. The bulk
action was handed stale rowids, disabled whoever owns them now (305 innocent users across ~20
resellers), left the actual target's users enabled — and, because the panel still answered 200,
recorded the action as a success.

Two guards, both asserted here:
  1. ids are ALWAYS resolved fresh from the panel; the durable cache is never a source of truth.
  2. a write that reports success but did not land on our users FAILS the action instead of
     silently finalizing (the miss must be loud).
"""
import asyncio
import datetime as dt
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/enfstale.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.models import (  # noqa: E402
    EndUserSnapshot,
    Invoice,
    Panel,
    Reseller,
)
from app.models.enums import (  # noqa: E402
    EnforcementActionStatus,
    EnforcementState,
    InvoiceStatus,
)


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await coro_fn(s, Session)
        finally:
            await engine.dispose()
    asyncio.run(go())


def _invoice(reseller_id):
    return Invoice(
        reseller_id=reseller_id, panel_id=1, period_start=dt.date(2026, 3, 1),
        period_end=dt.date(2026, 3, 31), period_label="2026-03", usage_gb=10,
        amount_toman=10000, amount_usdt=1, status=InvoiceStatus.sent,
        sent_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=5),
    )


async def _seed(s, *, stale_ids: bool):
    """Two users of reseller A. When `stale_ids`, their cached panel_user_id is the value the
    panel handed out BEFORE it was renumbered — it now belongs to another reseller's users."""
    from app.services import settings_service

    await settings_service.set_value(s, "enforcement_enabled", True)
    s.add(Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
    r = Reseller(panel_id=1, admin_uuid="A", name="R", panel_max_users=10,
                 panel_max_active_users=10, enforcement_state=EnforcementState.active)
    s.add(r)
    await s.flush()
    for i in range(2):
        s.add(EndUserSnapshot(
            panel_id=1, user_uuid=f"u{i}", name=f"u{i}", added_by_uuid="A", enable=True,
            panel_user_id=(900 + i) if stale_ids else None,   # 900/901 = the STALE ids
        ))
    inv = _invoice(r.id)
    s.add(inv)
    await s.commit()
    return r, inv


# The panel AFTER renumbering: our users now live at 10/11. 900/901 belong to somebody else.
FRESH = {"u0": 10, "u1": 11}


def test_stale_cached_panel_user_id_is_never_reused(tmp_path, monkeypatch):
    """The incident itself: with a poisoned durable cache the old code sent 900/901 (another
    reseller's rows) and never asked the panel. Now every id is resolved fresh, so ONLY the
    real ids 10/11 reach the bulk action."""
    from app.services import enforcement

    async def body(s, _Session):
        r, inv = await _seed(s, stale_ids=True)
        action = await enforcement.queue_enforcement(s, r, invoice_id=inv.id, dry_run=False)
        assert action.status == EnforcementActionStatus.planned

        looked_up: list[str] = []
        sent_ids: list[int] = []

        async def fake_user_id(self, panel, user_uuid, *, api_key=None):
            looked_up.append(user_uuid)
            return FRESH.get(user_uuid)

        async def fake_bulk(self, panel, user_ids, enabled):
            sent_ids.extend(user_ids)

        async def fake_get_user(self, panel, user_uuid, *, api_key=None):
            return {"enable": False}          # the write landed (verification passes)

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            return (10, 10)

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            return None

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_id", fake_user_id)
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_user", fake_get_user)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        await enforcement.process_enforcement_queue(s, action_limit=5, user_chunk_size=10)

        # Every target was re-resolved against the panel (the cache is not a source of truth).
        assert sorted(looked_up) == ["u0", "u1"], f"stale cache was reused: {looked_up}"
        # ONLY the fresh ids were written — the stale 900/901 never reached the panel.
        assert sorted(sent_ids) == [10, 11], f"wrong rowids sent: {sent_ids}"
        assert 900 not in sent_ids and 901 not in sent_ids

    _run(body, tmp_path, "stale_ids.db")


def test_bulk_write_that_missed_its_targets_fails_loudly(tmp_path, monkeypatch):
    """The silent-failure half: Hiddify answers 200 while changing nothing we intended. Without
    a read-back the action finalized as `done` (that is how the incident went unnoticed). Now a
    majority-wrong sample fails the action so it is visible and retryable."""
    from app.services import enforcement

    async def body(s, _Session):
        r, inv = await _seed(s, stale_ids=False)
        action = await enforcement.queue_enforcement(s, r, invoice_id=inv.id, dry_run=False)

        async def fake_user_id(self, panel, user_uuid, *, api_key=None):
            return FRESH.get(user_uuid)

        async def fake_bulk(self, panel, user_ids, enabled):
            return None                        # "succeeds" but changes nothing (the real bug)

        async def fake_get_user(self, panel, user_uuid, *, api_key=None):
            return {"enable": True}            # still enabled → the write missed

        async def fake_get_limits(self, panel, admin_uuid, api_key=None):
            return (10, 10)

        async def fake_set_limits(self, panel, admin_uuid, mu, mau, api_key=None):
            return None

        monkeypatch.setattr(enforcement.AdminApiClient, "get_user_id", fake_user_id)
        monkeypatch.setattr(enforcement.AdminApiClient, "bulk_set_users_enabled", fake_bulk)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_user", fake_get_user)
        monkeypatch.setattr(enforcement.AdminApiClient, "get_admin_limits", fake_get_limits)
        monkeypatch.setattr(enforcement.AdminApiClient, "set_admin_limits", fake_set_limits)

        await enforcement.process_enforcement_queue(s, action_limit=5, user_chunk_size=10)

        await s.refresh(action)
        assert action.status == EnforcementActionStatus.failed, (
            f"a write that missed every target was reported as {action.status}")
        assert "verification failed" in (action.error or "")
        # And the reseller was NOT left looking successfully enforced.
        await s.refresh(r)
        assert r.enforcement_state != EnforcementState.enforced

    _run(body, tmp_path, "missed_write.db")
