"""Panel `key` normalization + duplicate rejection.

The key is a short human label rendered wherever a panel is named (invoices, sales,
debts, portal) and it is copied into the durable `financial_records.panel_key`, so it
must be stable and unambiguous: `FA1`, `fa1 ` and `fa1` are ONE panel, not three.
"""
import asyncio
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/panelkey.db")
os.environ.setdefault("SECRET_KEY", "k")

from fastapi import BackgroundTasks, HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.api.panels import create_panel  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import Panel  # noqa: E402
from app.schemas.panel import PanelCreate  # noqa: E402

_UUID = "e5ed5732-1b80-489d-ac17-269d254e5c49"


def _body(key: str) -> PanelCreate:
    return PanelCreate(key=key, host="p.example.com", proxy_path="secret", owner_uuid=_UUID)


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


# ---- normalization (pure schema) ----

@pytest.mark.parametrize("raw,expected", [
    ("FA1", "fa1"),
    (" fa1 ", "fa1"),
    ("Fa1", "fa1"),
    ("fa1", "fa1"),
])
def test_key_is_stripped_and_lowercased(raw, expected):
    assert _body(raw).key == expected


def test_blank_key_is_rejected():
    # min_length=1 passes on "   ", so the validator must reject what stripping leaves.
    with pytest.raises(ValidationError):
        _body("   ")


# ---- duplicate rejection (API) ----

def test_duplicate_key_rejected_case_insensitively():
    async def body(s):
        # A legacy row stored before normalization existed.
        s.add(Panel(key="FA1", host="p.example.com", proxy_path_enc="x", owner_uuid=_UUID))
        await s.commit()
        with pytest.raises(HTTPException) as err:
            await create_panel(_body("fa1"), BackgroundTasks(), s)
        assert err.value.status_code == 409
        assert "fa1" in err.value.detail
        assert "وجود دارد" in err.value.detail  # user-facing message is Persian
    _run(body)


def test_distinct_keys_are_accepted():
    async def body(s):
        await create_panel(_body("fa1"), BackgroundTasks(), s)
        await create_panel(_body("fa2"), BackgroundTasks(), s)
        keys = sorted(p.key for p in (await s.execute(Panel.__table__.select())).all())
        assert keys == ["fa1", "fa2"]
    _run(body)
