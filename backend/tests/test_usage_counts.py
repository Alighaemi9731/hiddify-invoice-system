"""«پُری ظرفیت» counts an admin's WHOLE subtree (itself + all descendants), all users (not just
active), case-insensitively, and independently per panel."""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/usage.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.api.resellers import _usage_counts  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import EndUserSnapshot, Panel, Reseller  # noqa: E402


def _run(body):
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await body(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


def _users(panel_id, added_by, total, active):
    rows = []
    for i in range(total):
        rows.append(EndUserSnapshot(
            panel_id=panel_id, user_uuid=f"{added_by}-u{i}", added_by_uuid=added_by,
            enable=(i < active), is_active=(i < active)))
    return rows


def test_subtree_counts_all_users_case_insensitive_per_panel():
    async def body(s):
        p1 = Panel(key="p1", host="p1", proxy_path_enc="x", owner_uuid="o1")
        p2 = Panel(key="p2", host="p2", proxy_path_enc="x", owner_uuid="o2")
        s.add_all([p1, p2])
        await s.flush()
        # p1 tree:  A → B → C  (and unrelated D)
        s.add_all([
            Reseller(panel_id=p1.id, admin_uuid="A-UUID", name="A"),
            Reseller(panel_id=p1.id, admin_uuid="B-UUID", parent_admin_uuid="A-UUID", name="B"),
            Reseller(panel_id=p1.id, admin_uuid="C-UUID", parent_admin_uuid="B-UUID", name="C"),
            Reseller(panel_id=p1.id, admin_uuid="D-UUID", name="D"),
            Reseller(panel_id=p2.id, admin_uuid="A-UUID", name="A-on-p2"),  # same uuid, other panel
        ])
        # own users: A=2 (1 active), B=3 (all active), C=5 (2 active), D=1 (active). p2 A=7 (4).
        # NOTE: A's users are created under the LOWERCASE uuid → must still count (case-insensitive).
        s.add_all(_users(p1.id, "a-uuid", 2, 1))
        s.add_all(_users(p1.id, "B-UUID", 3, 3))
        s.add_all(_users(p1.id, "C-UUID", 5, 2))
        s.add_all(_users(p1.id, "D-UUID", 1, 1))
        s.add_all(_users(p2.id, "A-UUID", 7, 4))
        await s.commit()

        counts = await _usage_counts(s, None)
        # A = own(2) + B(3) + C(5) = 10 total; active = 1+3+2 = 6  (inactive ARE counted in total)
        assert counts[(p1.id, "A-UUID")] == (10, 6)
        assert counts[(p1.id, "B-UUID")] == (8, 5)     # B + C
        assert counts[(p1.id, "C-UUID")] == (5, 2)     # leaf
        assert counts[(p1.id, "D-UUID")] == (1, 1)     # no subtree → unchanged
        # Same uuid on p2 is independent.
        assert counts[(p2.id, "A-UUID")] == (7, 4)

        # Scoped to one panel returns the same p1 numbers.
        scoped = await _usage_counts(s, p1.id)
        assert scoped[(p1.id, "A-UUID")] == (10, 6)
        assert (p2.id, "A-UUID") not in scoped
    _run(body)


def test_owner_capacity_bar_ignores_deleted_but_retained_users():
    """The «پُری ظرفیت» bar must agree with the capacity the reseller can actually use.

    Same operational semantics as `usercreate.current_user_count`: snapshots retained for billing
    after the user was deleted from Hiddify occupy no slot, so showing them made the owner's bar
    read fuller than the panel really was (and disagree with the creation guard).
    """
    import datetime as dt

    async def body(s):
        synced = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc)
        s.add(Panel(id=1, key="p1", host="h1", proxy_path_enc="x", owner_uuid="o",
                    last_synced_at=synced))
        s.add(Reseller(panel_id=1, admin_uuid="A", name="A", parent_admin_uuid="o"))
        for i in range(2):   # present in the latest sync
            s.add(EndUserSnapshot(panel_id=1, user_uuid=f"fresh{i}", added_by_uuid="A",
                                  enable=True, is_active=True, last_synced_at=synced))
        for i in range(3):   # deleted from Hiddify, row retained for billing
            s.add(EndUserSnapshot(panel_id=1, user_uuid=f"stale{i}", added_by_uuid="A",
                                  enable=True, is_active=True,
                                  last_synced_at=synced - dt.timedelta(days=2)))
        await s.commit()
        counts = await _usage_counts(s, None)
        assert counts[(1, "A")] == (2, 2), "retained deleted users inflated the capacity bar"
    _run(body)


def test_usage_counts_fail_open_when_panel_never_synced():
    """No trustworthy sync → count everything rather than pretend users were deleted."""
    async def body(s):
        s.add(Panel(id=1, key="p1", host="h1", proxy_path_enc="x", owner_uuid="o"))
        s.add(Reseller(panel_id=1, admin_uuid="A", name="A", parent_admin_uuid="o"))
        s.add_all(_users(1, "A", 4, 2))
        await s.commit()
        counts = await _usage_counts(s, None)
        assert counts[(1, "A")] == (4, 2)
    _run(body)
