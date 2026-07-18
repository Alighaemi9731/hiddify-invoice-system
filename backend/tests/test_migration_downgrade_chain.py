"""Every migration must be reversible — rollback depends on it.

`deploy/rollback.sh` restores older CODE, but the database keeps whatever schema the newer release
migrated it to. Alembic then refuses to start, because the revision recorded in `alembic_version` no
longer exists in the older tree: `CommandError: Can't locate revision identified by '<head>'`. That
is raised from `core/db.py::_upgrade_schema` on boot, nothing catches it, and `restart: unless-stopped`
turns it into an infinite restart loop on BOTH the backend and the bot containers.

The fix is for rollback to run `alembic downgrade <target_head>` before swapping the code — which is
only viable while every revision in between actually reverses. These tests make that a permanent,
enforced property so it cannot rot: a new migration with a stub `downgrade()` fails CI here rather
than silently removing the ability to roll back past it.

SQLite is enough for the structural property (the chain resolves and each body executes). The
Postgres behaviour — where `batch_alter_table` renders as real `ALTER TABLE` instead of a table
rebuild — is covered by the `pg_contract` case at the bottom, which CI runs against postgres:16.
"""
from __future__ import annotations

import ast
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.pg_barrier import requires_pg

BACKEND_DIR = Path(__file__).resolve().parents[1]
VERSIONS_DIR = BACKEND_DIR / "alembic" / "versions"
ALEMBIC = str(Path(sys.executable).with_name("alembic"))
BASELINE = "18a3b4fd6e33"

# Revisions whose downgrade is legitimately a no-op: each is a one-way DATA normalization, not a
# schema change, so there is nothing to reverse and rollback across them is still safe. Each must
# say so in its own docstring/comment — asserted below. Adding a revision here is a deliberate act.
_DOCUMENTED_NOOP_DOWNGRADES = {
    "3f2a7c91b8e4",  # obsolete enum labels normalized; original label is unrecoverable
    "b1c3e5a7f9d2",  # uuids lowercased; original casing is unrecoverable
    "c2d4f6b8a1e3",  # TON hex txids lowercased; original casing is unrecoverable
    "d5b8e3f2a017",  # storefront enabled backfill; prior per-row state is unknowable
}


def _alembic(db_url: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": db_url}
    return subprocess.run(
        [ALEMBIC, *args], cwd=BACKEND_DIR, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check,
    )


def _revision_files() -> list[Path]:
    return sorted(p for p in VERSIONS_DIR.glob("*.py") if not p.name.startswith("__"))


def _downgrade_body(path: Path) -> list[ast.stmt]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            return node.body
    raise AssertionError(f"{path.name} has no downgrade() at all")


def _is_stub(body: list[ast.stmt]) -> bool:
    """True when the body is only a docstring and/or `pass` — i.e. it reverses nothing."""
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # docstring
        return False
    return True


def test_every_revision_downgrades_to_base(tmp_path):
    """Upgrade to head, then walk the WHOLE chain back down. This is the gate: if it fails, the
    rollback procedure in deploy/rollback.sh cannot work and must be redesigned."""
    db = tmp_path / "chain.db"
    url = f"sqlite+aiosqlite:///{db}"
    _alembic(url, "upgrade", "head")

    conn = sqlite3.connect(db)
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone() is not None
    conn.close()

    _alembic(url, "downgrade", "base")

    # `downgrade base` drops alembic_version's row (the table itself may remain).
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT version_num FROM alembic_version").fetchall()
    conn.close()
    assert rows == [], f"chain did not unwind cleanly: {rows}"


def test_one_step_down_from_head_is_reversible(tmp_path):
    """The realistic rollback: exactly one release back, then forward again. A rollback that cannot
    be undone is worse than no rollback."""
    db = tmp_path / "onestep.db"
    url = f"sqlite+aiosqlite:///{db}"
    _alembic(url, "upgrade", "head")
    head = _alembic(url, "current").stdout

    _alembic(url, "downgrade", "-1")
    after = _alembic(url, "current").stdout
    assert after != head, "downgrade -1 did not move the revision"

    _alembic(url, "upgrade", "head")
    assert _alembic(url, "current").stdout.split()[0] == head.split()[0]


def test_no_undocumented_stub_downgrade():
    """A new migration must actually reverse itself, or be explicitly listed as a documented
    one-way data normalization. This is what keeps the chain above from rotting."""
    offenders = []
    for path in _revision_files():
        revision = path.name.split("_", 1)[0]
        if _is_stub(_downgrade_body(path)) and revision not in _DOCUMENTED_NOOP_DOWNGRADES:
            offenders.append(path.name)
    assert not offenders, (
        "these migrations have an empty downgrade() and are not in the documented no-op set — "
        "rolling back past them is impossible:\n  " + "\n  ".join(offenders)
    )


def test_documented_noop_downgrades_explain_themselves():
    """Every entry in the allow-list must justify itself in its own file, so a future reader learns
    WHY it cannot be reversed instead of assuming someone was lazy."""
    for revision in _DOCUMENTED_NOOP_DOWNGRADES:
        matches = [p for p in _revision_files() if p.name.startswith(revision)]
        assert matches, f"{revision} is allow-listed but no such migration exists"
        text = matches[0].read_text(encoding="utf-8")
        tail = text[text.index("def downgrade"):]
        assert '"""' in tail or "#" in tail, (
            f"{matches[0].name}: no-op downgrade with no explanation of why it is irreversible"
        )


def test_stub_detector_actually_detects_a_stub(tmp_path):
    """Guard the guard: a detector that silently matches nothing would make the test above
    vacuously green forever."""
    stub = tmp_path / "abc123_stub.py"
    stub.write_text('def downgrade():\n    """Nothing to do."""\n    pass\n', encoding="utf-8")
    assert _is_stub(_downgrade_body(stub))

    real = tmp_path / "def456_real.py"
    real.write_text('def downgrade():\n    op.drop_table("x")\n', encoding="utf-8")
    assert not _is_stub(_downgrade_body(real))


@pytest.mark.pg_contract
@requires_pg
def test_downgrade_chain_unwinds_on_real_postgres():
    """The property that actually matters in production.

    On SQLite `batch_alter_table` rebuilds the table, which hides ordering and constraint problems
    that real `ALTER TABLE`/`DROP CONSTRAINT` on Postgres would surface. Production is Postgres 16,
    so the chain is proven there too before rollback is allowed to rely on it.
    """
    url = os.environ["DATABASE_URL"]
    _alembic(url, "upgrade", "head")
    head = _alembic(url, "current").stdout.split()[0]

    _alembic(url, "downgrade", "-1")          # the realistic one-release rollback
    _alembic(url, "upgrade", "head")          # …and forward again
    assert _alembic(url, "current").stdout.split()[0] == head

    _alembic(url, "downgrade", "base")        # the whole way down
    _alembic(url, "upgrade", "head")          # leave the DB usable for other pg tests
    assert _alembic(url, "current").stdout.split()[0] == head
