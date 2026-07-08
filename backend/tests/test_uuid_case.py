"""H05 — uuid case normalization.

- parse_backup lowercases admin/user uuids at ingest, so a case-mismatched
  parent_admin_uuid / added_by_uuid can no longer detach a subtree from its billing bundle.
- The data migration lowercases existing rows and merges case-duplicate resellers.
"""
import datetime as dt
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/uuidcase.db")
os.environ.setdefault("SECRET_KEY", "k")

from app.services.invoice_engine import (  # noqa: E402
    compute_invoices,
    select_billable_roots,
)
from app.services.panel_client.base import parse_backup  # noqa: E402
from app.services.periods import month_period  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALEMBIC = str(Path(sys.executable).with_name("alembic"))


def test_parse_backup_lowercases_uuids():
    payload = {
        "admin_users": [
            {"uuid": "OWNER", "name": "o", "is_super_admin": True},
            {"uuid": "ABC123", "name": "R1", "parent_admin_uuid": "OWNER"},
            {"uuid": "def456", "name": "R1a", "parent_admin_uuid": "abc123"},
        ],
        "users": [
            {"uuid": "U1", "name": "u1", "added_by_uuid": "ABC123",
             "usage_limit_GB": 10, "start_date": "2026-02-15"},
            {"uuid": "u2", "name": "u2", "added_by_uuid": "DEF456",
             "usage_limit_GB": 20, "start_date": "2026-02-15"},
        ],
    }
    data = parse_backup(payload)
    assert {a.uuid for a in data.admins} == {"owner", "abc123", "def456"}
    assert data.admins[1].parent_admin_uuid == "owner"
    assert data.admins[2].parent_admin_uuid == "abc123"
    assert {u.added_by_uuid for u in data.users} == {"abc123", "def456"}


def test_normalized_data_bundles_mixed_case_subtree():
    """After ingest normalization, a sub whose parent casing differed still bundles under its
    parent (the bug: a case-mismatched parent detached the subtree → silently unbilled)."""
    from types import SimpleNamespace

    def R(uuid, parent, **kw):
        return SimpleNamespace(admin_uuid=uuid, parent_admin_uuid=parent, is_owner=kw.get("owner", False),
                               exclude_from_billing=False, price_per_gb=None, name=uuid,
                               id=hash(uuid) & 0xffff, min_sale_toman=None)

    def U(uuid, added_by, gb):
        return SimpleNamespace(user_uuid=uuid, added_by_uuid=added_by, name=uuid,
                               start_date=dt.date(2026, 2, 15), usage_limit_gb=gb)

    # Simulate POST-ingest (already lowercased) rows — what the engine now always sees.
    resellers = [R("owner", None, owner=True), R("r1", "owner"), R("r1a", "r1")]
    users = [U("u1", "r1", 10), U("u2", "r1a", 20)]
    period = month_period(2026, 2)
    bundles = compute_invoices(
        resellers, users, period, default_price_per_gb=1000, excluded_usage_gb=set())
    roots = {r.admin_uuid for r in select_billable_roots(resellers)}
    assert roots == {"r1"}
    r1_bundle = next(b for b in bundles if b.root.admin_uuid == "r1")
    assert r1_bundle.total_gb == 30  # own 10 + sub 20 — the sub is NOT lost


def _seed_mixed_case_db(db: Path) -> None:
    """Upgrade to the pre-H05 head, then seed mixed-case rows via the ORM (H05 adds no
    columns, so the current models match that schema)."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import EndUserSnapshot, Panel, Reseller

    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    subprocess.run([ALEMBIC, "upgrade", "a3c5e7b9d1f2"], cwd=BACKEND_DIR, env=env,
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as s:
            s.add(Panel(id=1, key="p", name="p", host="h", proxy_path_enc="x", owner_uuid="owner"))
            await s.flush()
            # Case-duplicate pair: 'ABC' (0 invoices) + 'abc' (has bot_chat_id → keeper).
            s.add(Reseller(id=1, panel_id=1, admin_uuid="ABC", name="Big",
                           parent_admin_uuid="OWNER"))
            s.add(Reseller(id=2, panel_id=1, admin_uuid="abc", name="small",
                           parent_admin_uuid="owner", bot_chat_id=555, price_per_gb=2000))
            s.add(Reseller(id=3, panel_id=1, admin_uuid="sub1", name="Sub",
                           parent_admin_uuid="Abc"))
            s.add(EndUserSnapshot(
                panel_id=1, user_uuid="U9", name="u9", added_by_uuid="ABC",
                usage_limit_gb=5, current_usage_gb=0,
                last_synced_at=dt.datetime(2026, 2, 1)))
            s.add(EndUserSnapshot(
                panel_id=1, user_uuid="u9", name="u9", added_by_uuid="abc",
                usage_limit_gb=7, current_usage_gb=0,
                last_synced_at=dt.datetime(2026, 3, 1)))
            await s.commit()
        await engine.dispose()

    asyncio.run(go())


def test_migration_merges_case_duplicates_and_lowercases(tmp_path):
    db = tmp_path / "mixed.db"
    _seed_mixed_case_db(db)
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    subprocess.run([ALEMBIC, "upgrade", "head"], cwd=BACKEND_DIR, env=env,
                   check=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    conn = sqlite3.connect(db)
    # The two case-duplicate resellers merged into one (keeper #2 has the bot_chat_id).
    reseller_rows = conn.execute(
        "SELECT id, admin_uuid, parent_admin_uuid, bot_chat_id, price_per_gb FROM resellers "
        "ORDER BY id").fetchall()
    uuids = {r[1] for r in reseller_rows}
    assert "ABC" not in uuids and "abc" in uuids            # lowercased + deduped
    keeper = next(r for r in reseller_rows if r[1] == "abc")
    assert keeper[3] == 555                                  # kept its bot
    # The sub's parent was lowercased to match.
    sub = next(r for r in reseller_rows if r[1] == "sub1")
    assert sub[2] == "abc"
    # Snapshot dedup kept the latest (7 GB, synced in March), lowercased.
    snaps = conn.execute(
        "SELECT user_uuid, added_by_uuid, usage_limit_gb FROM end_user_snapshots").fetchall()
    assert len(snaps) == 1
    assert snaps[0][0] == "u9" and snaps[0][1] == "abc" and float(snaps[0][2]) == 7
    conn.close()
