"""B07 versioned migrations and input-contract regressions."""
import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import resellers as resellers_api
from app.api import settings as settings_api
from app.core import crypto
from app.models import Panel, Reseller
from app.models.enums import (
    DeliveryStatus,
    EnforcementActionStatus,
    EnforcementActionType,
    EnforcementState,
    PaymentMethod,
    PaymentStatus,
    SyncSource,
)
from app.schemas.invoice import GenerateRequest, InvoiceDetail, InvoiceEdit
from app.schemas.reseller import BumpLimitsBody, ResellerUpdate
from app.schemas.setting import SettingsBulkUpdate
from app.services import settings_service

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALEMBIC = str(Path(sys.executable).with_name("alembic"))
BASELINE = "18a3b4fd6e33"
HEAD = "9b1e4c72a5f8"


def _alembic(db_path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}"}
    return subprocess.run(
        [ALEMBIC, *args], cwd=BACKEND_DIR, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check,
    )


def test_fresh_database_migrates_to_head_with_constraints(tmp_path):
    db = tmp_path / "fresh.db"
    _alembic(db, "upgrade", "head")
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD,)
    invoice_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='invoices'"
    ).fetchone()[0]
    assert "ck_invoices_usage_nonnegative" in invoice_sql
    assert "ck_invoices_toman_nonnegative" in invoice_sql
    conn.close()


def test_existing_compatible_schema_is_stamped_then_upgraded(tmp_path):
    db = tmp_path / "existing.db"
    _alembic(db, "upgrade", BASELINE)
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE alembic_version")
    conn.commit()
    conn.close()

    result = _alembic(db, "upgrade", "head")
    assert "stamped baseline revision" in result.stdout
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (HEAD,)
    conn.close()


def test_obsolete_enum_rows_are_normalized_before_app_load(tmp_path):
    db = tmp_path / "legacy-enums.db"
    _alembic(db, "upgrade", "6a9c7f21d4e0")
    conn = sqlite3.connect(db)
    conn.executemany(
        """
        INSERT INTO panels
            (id, key, name, host, proxy_path_enc, owner_uuid, enabled, status, source)
        VALUES (?, ?, '', 'panel.local', 'x', 'owner', 1, 'ok', ?)
        """,
        [(1, "p1", "admin_api"), (2, "p2", "sample")],
    )
    conn.executemany(
        """
        INSERT INTO sync_runs
            (id, panel_id, source, status, admin_count, user_count, started_at)
        VALUES (?, 1, ?, 'success', 0, 0, CURRENT_TIMESTAMP)
        """,
        [(1, "admin_api"), (2, "sample")],
    )
    conn.execute(
        """
        INSERT INTO resellers
            (id, panel_id, admin_uuid, name, mode, is_owner, exclude_from_billing,
             can_add_admin, enforcement_state)
        VALUES (1, 1, 'r1', 'R', 'agent', 0, 0, 0, 'warned')
        """
    )
    conn.execute(
        """
        INSERT INTO payments
            (id, reseller_id, method, status, chain, confirmations, amount_usdt)
        VALUES (1, 1, 'usdt_hd', 'duplicate', 'bsc', 0, 0)
        """
    )
    conn.execute(
        """
        INSERT INTO delivery_log
            (id, reseller_id, kind, channel, status, created_at)
        VALUES (1, 1, 'generic', 'telegram', 'skipped', CURRENT_TIMESTAMP)
        """
    )
    conn.executemany(
        """
        INSERT INTO enforcement_actions
            (id, reseller_id, action, status, dry_run, affected_count, created_at)
        VALUES (?, 1, ?, 'done', 0, 0, CURRENT_TIMESTAMP)
        """,
        [(1, "warn"), (2, "zero_limits")],
    )
    conn.commit()
    conn.close()

    _alembic(db, "upgrade", "head")
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT DISTINCT source FROM panels").fetchall() == [("backup_json",)]
    assert conn.execute("SELECT DISTINCT source FROM sync_runs").fetchall() == [("backup_json",)]
    assert conn.execute("SELECT method, status FROM payments").fetchone() == (
        "usdt_txid", "rejected",
    )
    assert conn.execute("SELECT status FROM delivery_log").fetchone() == ("failed",)
    assert conn.execute("SELECT enforcement_state FROM resellers").fetchone() == ("active",)
    assert conn.execute("SELECT DISTINCT action FROM enforcement_actions").fetchall() == [
        ("disable_users",)
    ]
    conn.close()


def test_incomplete_existing_schema_refuses_baseline(tmp_path):
    db = tmp_path / "broken.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE app_users (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    result = _alembic(db, "upgrade", "head", check=False)
    assert result.returncode != 0
    assert "Refusing to baseline an incomplete existing database" in result.stdout


def test_programmatic_migration_preserves_application_logging(tmp_path):
    db = tmp_path / "logging.db"
    script = """
import logging

probe = logging.getLogger("app.migration_logging_probe")
probe.setLevel(logging.INFO)
handler = logging.StreamHandler()
probe.addHandler(handler)

from app.core.db import _upgrade_schema

_upgrade_schema()
assert probe.disabled is False
assert probe.level == logging.INFO
assert handler in probe.handlers
"""
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND_DIR,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=True,
    )


def test_active_enum_contract_has_no_unimplemented_branches():
    assert {item.value for item in SyncSource} == {"backup_json"}
    assert {item.value for item in PaymentMethod} == {
        "usdt_txid", "manual", "screenshot", "ton_txid",
    }
    assert {item.value for item in PaymentStatus} == {"pending", "confirmed", "rejected"}
    assert {item.value for item in DeliveryStatus} == {
        "sent", "failed", "blocked", "unmatched",
    }
    assert {item.value for item in EnforcementState} == {"active", "enforced"}
    assert {item.value for item in EnforcementActionType} == {"disable_users", "restore"}
    assert {item.value for item in EnforcementActionStatus} == {
        "planned", "running", "partial", "dry_run", "done", "failed", "reverted",
    }


def test_financial_and_mutable_default_contracts():
    with pytest.raises(ValidationError):
        InvoiceEdit(usage_gb=-1)
    with pytest.raises(ValidationError):
        InvoiceEdit(amount_toman=float("nan"))
    with pytest.raises(ValidationError):
        ResellerUpdate(price_per_gb=-1)
    with pytest.raises(ValidationError):
        BumpLimitsBody(amount=0)
    with pytest.raises(ValidationError):
        GenerateRequest(period="2026-13")

    first = InvoiceDetail.model_construct(lines=[])
    second = InvoiceDetail.model_construct()
    first.lines.append(object())
    assert second.lines == []


def test_setting_allowlist_types_and_ranges():
    assert settings_service.validate_api_value("invoice_hour", 23) == 23
    assert settings_service.validate_api_value("sync_interval_hours", 24) == 24
    assert settings_service.validate_api_value("guard_interval_minutes", 60) == 60
    assert settings_service.validate_api_value("rate_refresh_hours", 24) == 24
    assert settings_service.validate_api_value("excluded_usage_gb", [0, 1.5]) == [0.0, 1.5]
    for key, value in [
        ("unknown_key", 1),
        ("unknown_key", "••••"),
        ("owner_chat_id", "123"),
        ("invoice_hour", 24),
        ("invoice_hour", "9"),
        ("rate_mode", "automatic"),
        ("excluded_usage_gb", [-1]),
        ("overage_tolerance_gb", float("inf")),
    ]:
        with pytest.raises(ValueError):
            if value == "••••":
                settings_service.is_unchanged_secret_mask(key, value)
            else:
                settings_service.validate_api_value(key, value)
    assert settings_service.is_unchanged_secret_mask("telegram_bot_token", "••••") is True
    assert settings_service.is_unchanged_secret_mask("owner_name", "••••") is False


def test_bulk_settings_validation_is_atomic(tmp_path):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
        from app.core.db import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            await settings_service.set_value(session, "invoice_hour", 9)
            body = SettingsBulkUpdate.model_validate({
                "items": [
                    {"key": "invoice_hour", "value": 8},
                    {"key": "unknown_key", "value": 1},
                ]
            })
            with pytest.raises(HTTPException) as exc:
                await settings_api.update_bulk(body, session)
            assert exc.value.status_code == 422
            assert await settings_service.get(session, "invoice_hour") == 9
        await engine.dispose()

    asyncio.run(run())


def test_reseller_tree_is_panel_scoped_and_cycle_safe(tmp_path):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tree.db'}")
        from app.core.db import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            p1 = Panel(
                key="p1", host="one", proxy_path_enc=crypto.encrypt("x"), owner_uuid="owner",
            )
            p2 = Panel(
                key="p2", host="two", proxy_path_enc=crypto.encrypt("x"), owner_uuid="owner",
            )
            session.add_all([p1, p2])
            await session.flush()
            session.add_all([
                Reseller(panel_id=p1.id, admin_uuid="OWNER", name="Owner 1", is_owner=True),
                Reseller(panel_id=p2.id, admin_uuid="OWNER", name="Owner 2", is_owner=True),
                Reseller(panel_id=p1.id, admin_uuid="A", parent_admin_uuid="owner", name="A"),
                Reseller(panel_id=p2.id, admin_uuid="B", parent_admin_uuid="owner", name="B"),
                Reseller(panel_id=p1.id, admin_uuid="C", parent_admin_uuid="D", name="C"),
                Reseller(panel_id=p1.id, admin_uuid="D", parent_admin_uuid="C", name="D"),
            ])
            await session.commit()
            tree = await resellers_api.reseller_tree(panel_id=None, q=None, session=session)
            names = {node["name"] for node in tree}
            assert {"A", "B"} <= names
            cyclic = [node for node in tree if node["name"] in {"C", "D"}]
            assert len(cyclic) == 1
            assert cyclic[0]["cycle_detected"] is True
        await engine.dispose()

    asyncio.run(run())
