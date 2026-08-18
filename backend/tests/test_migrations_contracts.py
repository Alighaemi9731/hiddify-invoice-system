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
HEAD = "a7c1e9d3b5f2"


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


def test_storefront_admin_audit_command_schema_contract(tmp_path):
    db = tmp_path / "storefront-admin.db"
    _alembic(db, "upgrade", "head")
    conn = sqlite3.connect(db)
    bot_columns = {
        row[1]: row for row in conn.execute("PRAGMA table_info(storefront_bots)").fetchall()
    }
    assert bot_columns["config_version"][3] == 1
    assert str(bot_columns["config_version"][4]).strip("'\"") == "1"
    assert {"channel_verified_at", "channel_verification_error"} <= set(bot_columns)
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"storefront_audit_events", "storefront_api_commands"} <= tables
    command_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='storefront_api_commands'"
    ).fetchone()[0]
    assert "uq_sfcommand_actor_key" in command_sql
    assert "ck_sfcommand_status" in command_sql
    indexes = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    assert {
        "ix_sfaudit_shop_created", "ix_sfaudit_actor_action", "ix_sfaudit_entity",
        "ix_sfcommand_status_lease", "ix_sfcommand_shop_updated",
    } <= indexes
    conn.close()


def test_storefront_credits_communications_schema_contract(tmp_path):
    """Plan 006: `archived_at` on credit codes, the two durable-delivery tables + their indexes /
    unique / status CHECK all reach a fresh HEAD database."""
    db = tmp_path / "sf-comms.db"
    _alembic(db, "upgrade", "head")
    conn = sqlite3.connect(db)
    credit_cols = {row[1] for row in conn.execute("PRAGMA table_info(storefront_credit_codes)")}
    assert "archived_at" in credit_cols
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"storefront_broadcast_jobs", "storefront_delivery_recipients"} <= tables
    recip_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='storefront_delivery_recipients'"
    ).fetchone()[0]
    assert "uq_sfdr_job_customer" in recip_sql and "ck_sfdr_status" in recip_sql
    job_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='storefront_broadcast_jobs'"
    ).fetchone()[0]
    assert "ck_sfbjob_kind" in job_sql and "ck_sfbjob_status" in job_sql
    indexes = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"ix_sfdr_claimable", "ix_sfdr_job_status", "ix_sfbjob_shop_created"} <= indexes
    conn.close()


def test_reseller_crm_schema_contract(tmp_path):
    """The follow-up board's two tables, their indexes, and the `muted` boolean default all
    reach a fresh HEAD database. `muted` must default to a real false — a NULL/absent default
    would make an untouched reseller's state row unfilterable in the "due" view."""
    db = tmp_path / "crm.db"
    _alembic(db, "upgrade", "head")
    conn = sqlite3.connect(db)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"reseller_crm_state", "reseller_followups"} <= tables
    state_cols = {row[1]: row for row in conn.execute("PRAGMA table_info(reseller_crm_state)")}
    assert {"reseller_id", "snoozed_until", "muted", "last_touch_at", "touch_count",
            "note"} <= set(state_cols)
    assert state_cols["muted"][3] == 1                                  # NOT NULL
    assert str(state_cols["muted"][4]).strip("'\"").lower() in {"0", "false"}
    assert str(state_cols["touch_count"][4]).strip("'\"") == "0"
    followup_cols = {row[1] for row in conn.execute("PRAGMA table_info(reseller_followups)")}
    assert {"reseller_id", "reseller_admin_uuid", "reseller_name", "panel_key", "segment",
            "note", "snoozed_until", "muted", "actor"} <= followup_cols
    indexes = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"ix_crmfollowup_reseller_created", "ix_crmfollowup_created",
            "ix_reseller_crm_state_reseller_id"} <= indexes
    # SET NULL, not CASCADE: an outreach record must outlive the reseller row it points at.
    followup_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='reseller_followups'"
    ).fetchone()[0]
    assert "SET NULL" in followup_sql
    conn.close()


def test_storefront_trial_reset_schema_contract(tmp_path):
    """The monthly free-trial re-arm stamp reaches a fresh HEAD database. NULLable on purpose:
    NULL means "never reset", which is the state every pre-existing shop is in."""
    db = tmp_path / "sf-trial-reset.db"
    _alembic(db, "upgrade", "head")
    conn = sqlite3.connect(db)
    cols = {row[1]: row for row in conn.execute("PRAGMA table_info(storefront_bots)")}
    assert "trial_reset_period" in cols
    assert cols["trial_reset_period"][3] == 0            # nullable
    assert cols["trial_reset_period"][2].upper().startswith("VARCHAR")
    conn.close()


def test_broadcast_job_kind_admits_trial_reset(tmp_path):
    """The platform's monthly free-trial notice rides the delivery queue as its own `kind`.

    Both checks are asserted, not just the widened one: the widening is done in SQLite's batch
    mode, which REBUILDS the table, and a reflected CHECK comes back unnamed — so a careless
    rebuild silently drops `ck_sfbjob_status` and nothing else in the suite would notice."""
    db = tmp_path / "sf-job-kind.db"
    _alembic(db, "upgrade", "head")
    conn = sqlite3.connect(db)
    job_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='storefront_broadcast_jobs'"
    ).fetchone()[0]
    assert "ck_sfbjob_kind" in job_sql and "ck_sfbjob_status" in job_sql
    assert "trial_reset" in job_sql
    # No parent rows needed: sqlite3 leaves foreign keys OFF, so the CHECK is what's under test.
    for kind in ("broadcast", "direct", "trial_reset"):
        conn.execute(
            "INSERT INTO storefront_broadcast_jobs "
            "(storefront_bot_id, actor_telegram_id, kind, message_text, status) "
            "VALUES (1, 1, ?, 't', 'queued')", (kind,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO storefront_broadcast_jobs "
            "(storefront_bot_id, actor_telegram_id, kind, message_text, status) "
            "VALUES (1, 1, 'nonsense', 't', 'queued')")
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
        "usdt_txid", "manual", "screenshot", "ton_txid", "avax_txid",
    }
    assert {item.value for item in PaymentStatus} == {"pending", "confirmed", "rejected"}
    assert {item.value for item in DeliveryStatus} == {
        "sent", "failed", "blocked", "unmatched",
    }
    assert {item.value for item in EnforcementState} == {"active", "frozen", "enforced"}
    assert {item.value for item in EnforcementActionType} == {
        "disable_users", "restore", "delete_admin", "freeze",
    }
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
    assert settings_service.validate_api_value("storefront_delivery_worker_interval_minutes", 60) == 60
    assert settings_service.validate_api_value("storefront_delivery_retention_days", 3650) == 3650
    for key, value in [
        ("unknown_key", 1),
        ("unknown_key", "••••"),
        ("owner_chat_id", "123"),
        ("invoice_hour", 24),
        ("invoice_hour", "9"),
        ("rate_mode", "automatic"),
        ("excluded_usage_gb", [-1]),
        ("overage_tolerance_gb", float("inf")),
        ("storefront_delivery_worker_interval_minutes", 61),
        ("storefront_delivery_retention_days", 3651),
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


def test_payment_settlements_backfill(tmp_path):
    """I06: upgrading past f7a3b5d9c2e4 backfills payment_settlements from the comma column
    (falling back to the primary invoice_id link) and skips dangling invoice ids."""
    db = tmp_path / "settle.db"
    _alembic(db, "upgrade", "e6d4a2c8b9f1")   # the revision just BEFORE the join table
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO panels (id, key, name, host, proxy_path_enc, owner_uuid, enabled, status, source)"
        " VALUES (1, 'p', '', 'h', 'x', 'o', 1, 'ok', 'backup_json')"
    )
    conn.execute(
        "INSERT INTO resellers (id, panel_id, admin_uuid, name, mode, is_owner,"
        " exclude_from_billing, can_add_admin, enforcement_state)"
        " VALUES (1, 1, 'r', 'R', 'agent', 0, 0, 0, 'active')"
    )
    for iid, label in ((1, "2026-01"), (2, "2026-02")):
        conn.execute(
            "INSERT INTO invoices (id, reseller_id, panel_id, period_start, period_end,"
            " period_label, usage_gb, users_count, price_per_gb, amount_toman,"
            " base_amount_toman, min_sale_toman, floor_applied, usdt_rate, amount_usdt, status)"
            " VALUES (?, 1, 1, ?, ?, ?, 1, 1, 0, 0, 0, 0, 0, 0, 0, 'paid')",
            (iid, f"{label}-01", f"{label}-28", label),
        )
    conn.executemany(
        "INSERT INTO payments (id, reseller_id, invoice_id, method, status, chain,"
        " confirmations, amount_usdt, settled_invoice_ids)"
        " VALUES (?, 1, ?, 'manual', 'confirmed', 'bsc', 0, 0, ?)",
        [
            (1, 1, "1,2"),    # multi-invoice comma set
            (2, 2, None),     # legacy row: only the primary invoice_id link
            (3, 1, "1,999"),  # 999 was deleted since → skipped, 1 kept
        ],
    )
    conn.commit()
    conn.close()

    _alembic(db, "upgrade", "head")
    conn = sqlite3.connect(db)
    rows = set(conn.execute(
        "SELECT payment_id, invoice_id FROM payment_settlements").fetchall())
    conn.close()
    assert rows == {(1, 1), (1, 2), (2, 2), (3, 1)}


def test_is_trial_backfill(tmp_path):
    """f1a2b3c4d5e6: upgrading past e4f7b1c9a2d5 backfills storefront_orders.is_trial=true for
    trial orders (plan_id NULL AND price_toman 0); paid orders (price>0 or a plan) stay false."""
    db = tmp_path / "istrial.db"
    _alembic(db, "upgrade", "e4f7b1c9a2d5")   # the revision just BEFORE is_trial
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO storefront_orders (id, customer_id, plan_id, gb, days, price_toman,"
        " status, created_at) VALUES (?, 1, ?, ?, 1, ?, 'provisioned', CURRENT_TIMESTAMP)",
        [
            (1, None, 1, 0),        # trial: plan_id NULL, price 0 -> is_trial
            (2, 5, 50, 100000),     # paid (has a plan) -> not trial
            (3, None, 20, 90000),   # paid whose plan was deleted (plan_id NULL but price>0) -> not trial
        ],
    )
    conn.commit()
    conn.close()

    _alembic(db, "upgrade", "head")
    conn = sqlite3.connect(db)
    rows = dict(conn.execute("SELECT id, is_trial FROM storefront_orders").fetchall())
    conn.close()
    assert rows == {1: 1, 2: 0, 3: 0}


def test_renewal_target_migration_raises_short_lease_floor(tmp_path):
    """a6c9e2f4b7d1 adds the durable renewal target and upgrades a legacy 180s lease to the safe 300s
    floor without requiring an operator to re-save Settings."""
    db = tmp_path / "renew-target.db"
    _alembic(db, "upgrade", "7968884fecbd")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO settings (key, value, is_secret, created_at, updated_at) "
        "VALUES ('storefront_operation_lease_seconds', '180', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )
    conn.commit()
    conn.close()

    _alembic(db, "upgrade", "head")
    conn = sqlite3.connect(db)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(storefront_operations)")}
    stored = conn.execute(
        "SELECT value FROM settings WHERE key='storefront_operation_lease_seconds'"
    ).fetchone()[0]
    conn.close()
    assert {"target_usage_limit_gb", "prior_panel_start_date"} <= columns
    assert int(stored) == 300
