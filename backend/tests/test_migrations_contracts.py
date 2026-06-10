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
from app.schemas.invoice import GenerateRequest, InvoiceDetail, InvoiceEdit
from app.schemas.reseller import BumpLimitsBody, ResellerUpdate
from app.schemas.setting import SettingsBulkUpdate
from app.services import settings_service

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALEMBIC = str(Path(sys.executable).with_name("alembic"))
BASELINE = "18a3b4fd6e33"
HEAD = "6a9c7f21d4e0"


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
