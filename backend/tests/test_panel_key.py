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
from sqlalchemy import select  # noqa: E402
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
        # Distinct keys AND distinct boxes. (`_body()` reuses one host+path, which is now correctly
        # refused as the same physical panel — see the duplicate-panel tests below.)
        await create_panel(_body("fa1"), BackgroundTasks(), s)
        await create_panel(
            PanelCreate(key="fa2", host="p2.example.com", proxy_path="secret2", owner_uuid=_UUID),
            BackgroundTasks(), s,
        )
        keys = sorted(p.key for p in (await s.execute(Panel.__table__.select())).all())
        assert keys == ["fa1", "fa2"]
    _run(body)


# ── the same PHYSICAL panel must never be registered twice ────────────────────
# Registering one box twice was silently accepted (only `key` was unique) and was quietly
# catastrophic: resellers are keyed by (panel_id, admin_uuid), so the second row rebuilt every
# reseller/user from scratch — one real person got TWO full invoices for the same traffic, reports
# doubled, and the bot's fail-closed link matching then saw two candidates and refused to register
# that reseller at all, blaming THEIR link.

def test_same_host_and_path_is_rejected_even_with_a_different_key():
    async def body(s):
        await create_panel(_body("fa1"), BackgroundTasks(), s)
        with pytest.raises(HTTPException) as err:
            await create_panel(_body("fa2"), BackgroundTasks(), s)   # same host+path, new key
        assert err.value.status_code == 409
        assert "fa1" in err.value.detail          # names the row it collides with
        assert (await s.execute(select(Panel))).scalars().all().__len__() == 1
    _run(body)


def test_a_genuinely_different_panel_is_still_accepted():
    async def body(s):
        await create_panel(_body("fa1"), BackgroundTasks(), s)
        other = PanelCreate(key="fa2", host="other.example.com", proxy_path="secret",
                            owner_uuid=_UUID)
        await create_panel(other, BackgroundTasks(), s)
        assert len((await s.execute(select(Panel))).scalars().all()) == 2
    _run(body)


def test_editing_a_host_cannot_collide_onto_another_panel():
    """The back door: the collision is equally reachable by EDITING an existing panel's host."""
    from app.api.panels import update_panel
    from app.schemas.panel import PanelUpdate

    async def body(s):
        await create_panel(_body("fa1"), BackgroundTasks(), s)
        second = PanelCreate(key="fa2", host="other.example.com", proxy_path="secret",
                             owner_uuid=_UUID)
        p2 = await create_panel(second, BackgroundTasks(), s)
        with pytest.raises(HTTPException) as err:
            await update_panel(p2.id, PanelUpdate(host="p.example.com"), BackgroundTasks(), s)
        assert err.value.status_code == 409
    _run(body)
