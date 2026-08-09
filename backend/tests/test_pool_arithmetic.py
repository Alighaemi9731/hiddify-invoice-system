"""Postgres `max_connections` must stay above what the app can actually open.

Why this file exists: three containers (backend, scheduler, bot) each build their own
SQLAlchemy engine, so the app's hard ceiling is `3 x (pool_size + max_overflow)` = 90
connections. The compose file used the postgres image default of 100, leaving ~10 for pg_dump,
the healthcheck and any manual psql — thin enough that a burst of storefront buyers could
exhaust it, and the failure mode is the whole panel refusing connections rather than one slow
query. Neither number referenced the other, so raising the pool would silently eat the margin.

This test ties them together: it reads the real pool settings out of `app/core/db.py` and the
real `max_connections` out of the production compose file, and fails if the margin disappears.
"""
from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
COMPOSE = BACKEND.parent / "deploy" / "docker-compose.prod.yml"

#: backend + scheduler + bot each own an engine. If a fourth ever appears, this must change.
APP_PROCESSES = 3
#: Room for pg_dump (backup + pre-restore safety dump), the pg_isready healthcheck, and a human.
REQUIRED_HEADROOM = 10


def _pool_settings() -> tuple[int, int]:
    src = (BACKEND / "app" / "core" / "db.py").read_text(encoding="utf-8")
    size = re.search(r'"pool_size":\s*(\d+)', src)
    overflow = re.search(r'"max_overflow":\s*(\d+)', src)
    assert size and overflow, "could not read the Postgres pool settings from app/core/db.py"
    return int(size.group(1)), int(overflow.group(1))


def _max_connections() -> int:
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(r"-c\s+max_connections=(\d+)", text)
    assert match, (
        "deploy/docker-compose.prod.yml no longer pins max_connections — it would fall back to "
        "the image default of 100, which is below what the app can open"
    )
    return int(match.group(1))


def test_app_cannot_exhaust_postgres_connections():
    pool_size, max_overflow = _pool_settings()
    app_ceiling = APP_PROCESSES * (pool_size + max_overflow)
    configured = _max_connections()
    assert configured >= app_ceiling + REQUIRED_HEADROOM, (
        f"max_connections={configured} but {APP_PROCESSES} processes x "
        f"({pool_size} pool + {max_overflow} overflow) = {app_ceiling} possible app connections, "
        f"leaving {configured - app_ceiling} for pg_dump/healthcheck/psql "
        f"(need >= {REQUIRED_HEADROOM}). Raise max_connections in deploy/docker-compose.prod.yml "
        "or shrink the pool in app/core/db.py."
    )


def test_work_mem_stays_modest_for_the_container_cap():
    """`work_mem` is per SORT, not per connection, so it multiplies by concurrency. With a 512m
    cap and up to ~90 backends, anything generous here is how Postgres gets OOM-killed."""
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(r"-c\s+work_mem=(\d+)MB", text)
    assert match, "work_mem is no longer pinned in deploy/docker-compose.prod.yml"
    assert int(match.group(1)) <= 8, (
        f"work_mem={match.group(1)}MB is too generous for the db container's 512m cap — it is "
        "allocated per sort/hash node, so a handful of concurrent reports can multiply it well "
        "past the cap"
    )


def test_shared_buffers_fits_the_container_cap():
    text = COMPOSE.read_text(encoding="utf-8")
    buffers = re.search(r"-c\s+shared_buffers=(\d+)MB", text)
    cap = re.search(r"db:.*?mem_limit:\s*(\d+)m", text, re.DOTALL)
    assert buffers and cap, "shared_buffers or the db mem_limit is no longer pinned"
    assert int(buffers.group(1)) <= int(cap.group(1)) // 3, (
        f"shared_buffers={buffers.group(1)}MB against a {cap.group(1)}m container cap leaves too "
        "little for backends, work_mem and the OS page cache"
    )
