"""«➕ نمایندهٔ جدید»: who the admin is created AS (billing!), what the panel is sent, and the
whole point of the feature — the reseller can register in the bot with NO sync in between."""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import runpy
import sys
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/rc.db")
os.environ.setdefault("SECRET_KEY", "k")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.bot import handlers  # noqa: E402
from app.bot.matching import parse_link  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import Panel, Reseller  # noqa: E402
from app.services import resellercreate, sync  # noqa: E402
from app.services.panel_client import admin_api  # noqa: E402
from app.services.panel_client.admin_api import (  # noqa: E402
    AGENT_CAPABILITIES,
    AdminExistsError,
    AdminVerificationError,
)
from app.services.panel_client.base import PanelAdmin, PanelData  # noqa: E402
from tests.test_panel_compat_reads import panel as api_panel  # noqa: E402
from tests.test_panel_compat_reads import transport

OWNER_UUID = "11111111-1111-4111-8111-111111111111"


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


def _panel(s, key: str = "p1", *, owner_uuid: str = OWNER_UUID) -> Panel:
    p = Panel(key=key, name=f"panel {key}", host=f"{key}.invalid", proxy_path_enc="x",
              owner_uuid=owner_uuid, enabled=True)
    p.proxy_path = "adminpath"
    s.add(p)
    return p


def _owner_row(s, panel: Panel) -> Reseller:
    """The panel's Owner row, as a sync would have written it."""
    r = Reseller(panel_id=panel.id, admin_uuid=OWNER_UUID, name="Owner", mode="super_admin",
                 is_owner=True, last_seen_at=dt.datetime.now(dt.timezone.utc))
    s.add(r)
    return r


def _spy_create_admin(monkeypatch) -> dict:
    seen: dict = {}

    async def fake(self, panel, *, name, api_key, parent_admin_uuid, admin_uuid=None,
                   max_users=100, max_active_users=100):  # noqa: ANN001
        seen.update(name=name, api_key=api_key, parent=parent_admin_uuid, uuid=admin_uuid,
                    max_users=max_users, max_active_users=max_active_users)
        return admin_uuid or "generated"

    monkeypatch.setattr(admin_api.AdminApiClient, "create_admin", fake)
    return seen


# ---------------- identity: the panel's OWN super-admin, top-level parent ----------------

def test_creates_as_and_under_the_panel_super_admin(monkeypatch):
    """`api_key` AND `parent_admin_uuid` must both be the panel's owner uuid.

    The header fallback in `_headers` is `panel.admin_api_key`, which may be a different admin.
    Creating under it would make the new reseller a SUB-reseller: barred from registering in the
    bot, and billed inside that other reseller's bundle."""
    seen = _spy_create_admin(monkeypatch)

    async def body(s):
        p = _panel(s)
        p.admin_api_key = "SOME-OTHER-ADMIN"  # must NOT be the identity we create with
        await s.commit()
        res = await resellercreate.create(s, p, name="نمایندهٔ تست")
        assert res.ok and res.admin_uuid
        assert seen["api_key"] == OWNER_UUID
        assert seen["parent"] == OWNER_UUID
        assert (seen["max_users"], seen["max_active_users"]) == (100, 100)
        assert res.link == p.admin_link(res.admin_uuid, tag="نمایندهٔ تست")
    _run(body)


def test_refuses_a_panel_without_a_super_admin_uuid(monkeypatch):
    async def never(self, panel, **kw):  # noqa: ANN001, ANN003
        raise AssertionError("must not reach the panel")

    monkeypatch.setattr(admin_api.AdminApiClient, "create_admin", never)

    async def body(s):
        p = _panel(s, owner_uuid="")
        await s.commit()
        res = await resellercreate.create(s, p, name="x")
        assert not res.ok and res.reason == "no_admin"
        assert (await s.execute(select(Reseller))).scalars().all() == []
    _run(body)


def test_never_sets_a_password(monkeypatch):
    """A non-empty password makes Hiddify reject UUID-link login (`auth_before_request`), i.e. the
    very link we hand the reseller would stop opening their panel."""
    _spy_create_admin(monkeypatch)

    async def never(self, *a, **kw):  # noqa: ANN001, ANN002, ANN003
        raise AssertionError("a new reseller must keep an EMPTY password")

    monkeypatch.setattr(admin_api.AdminApiClient, "set_admin_password", never)

    async def body(s):
        p = _panel(s)
        await s.commit()
        assert (await resellercreate.create(s, p, name="x")).ok
    _run(body)


# ---------------- the point of the feature: registration without a sync ----------------

def test_registers_locally_so_the_bot_accepts_the_link_with_no_sync(monkeypatch):
    _spy_create_admin(monkeypatch)

    async def body(s):
        p = _panel(s)
        await s.commit()
        _owner_row(s, p)
        await s.commit()

        res = await resellercreate.create(s, p, name="فلانی")
        assert res.ok and res.reseller_id

        row = await s.get(Reseller, res.reseller_id)
        assert row.admin_uuid == res.admin_uuid == res.admin_uuid.lower()
        assert (row.name, row.mode, row.is_owner) == ("فلانی", "agent", False)
        assert row.parent_admin_uuid == OWNER_UUID
        assert (row.panel_max_users, row.panel_max_active_users) == (100, 100)
        assert row.can_add_admin is False
        assert row.last_seen_at is not None

        # NO sync has run. The bot's registration matcher must still find exactly this row,
        # and must consider it top-level (only top-level resellers may register).
        parsed = parse_link(res.link)
        found = await handlers._registration_candidate(s, parsed)
        assert found is not None and found.id == row.id
        assert await handlers._is_top_level_reseller(s, found) is True
    _run(body)


def test_a_later_sync_updates_the_row_instead_of_twinning_it(monkeypatch):
    _spy_create_admin(monkeypatch)

    async def body(s):
        p = _panel(s)
        await s.commit()
        res = await resellercreate.create(s, p, name="فلانی")
        assert res.ok

        data = PanelData(admins=[
            PanelAdmin(uuid=OWNER_UUID, name="Owner", parent_admin_uuid=None, mode="super_admin",
                       comment=None, telegram_id=None, max_users=None, max_active_users=None),
            PanelAdmin(uuid=res.admin_uuid, name="فلانی", parent_admin_uuid=OWNER_UUID,
                       mode="agent", comment=None, telegram_id=None, max_users=100,
                       max_active_users=100),
        ])
        await sync._upsert_resellers(s, p, data, dt.datetime.now(dt.timezone.utc))
        await s.commit()

        rows = (await s.execute(
            select(Reseller).where(Reseller.admin_uuid == res.admin_uuid)
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].id == res.reseller_id
        assert (rows[0].name, rows[0].mode, rows[0].parent_admin_uuid) == (
            "فلانی", "agent", OWNER_UUID)
    _run(body)


# ---------------- failure paths ----------------

def test_panel_failures_are_reported_without_raising(monkeypatch):
    async def exists(self, panel, **kw):  # noqa: ANN001, ANN003
        raise AdminExistsError("The admin exists")

    async def boom(self, panel, **kw):  # noqa: ANN001, ANN003
        raise RuntimeError("panel down")

    async def body(s):
        p = _panel(s)
        await s.commit()
        monkeypatch.setattr(admin_api.AdminApiClient, "create_admin", exists)
        assert (await resellercreate.create(s, p, name="x")).reason == "exists"
        monkeypatch.setattr(admin_api.AdminApiClient, "create_admin", boom)
        assert (await resellercreate.create(s, p, name="x")).reason == "error"
        assert (await s.execute(select(Reseller))).scalars().all() == []
    _run(body)


def test_a_failed_local_save_still_returns_the_link(monkeypatch):
    """The admin exists on the panel by then; the next sync picks the row up. Losing the link the
    owner is waiting for would be the worse failure."""
    _spy_create_admin(monkeypatch)

    async def boom(session, panel, admin_uuid, name, owner_uuid):  # noqa: ANN001
        raise RuntimeError("db down")

    monkeypatch.setattr(resellercreate, "_register_locally", boom)

    async def body(s):
        p = _panel(s)
        await s.commit()
        res = await resellercreate.create(s, p, name="x")
        assert res.ok and res.link and res.saved is False
    _run(body)


# ---------------- name hygiene ----------------

@pytest.mark.parametrize("raw,expected", [
    ("  فلانی  ", "فلانی"),
    ("a\n  b", "a b"),
    ("", ""),
    (None, ""),
    ("/newadmin", ""),          # a mis-typed slash command is never a name
    ("x" * 200, "x" * resellercreate.NAME_MAX_LEN),
])
def test_clean_name(raw, expected):
    assert resellercreate.clean_name(raw) == expected


# ---------------- bot wiring ----------------

def test_owner_menu_label_opens_the_panel_picker():
    """The «➕ نمایندهٔ جدید» label must reach `_begin_new_reseller` and offer every enabled panel
    with the callback prefix the handler actually listens for."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot import keyboards

    assert keyboards.OWNER_LABEL_TO_ACTION["➕ نمایندهٔ جدید"] == "newadmin"

    class _Msg:
        def __init__(self) -> None:
            self.sent: list = []

        async def answer(self, text, **kw):  # noqa: ANN001, ANN003
            self.sent.append((text, kw))

    async def body(s):
        _panel(s, "p1")
        _panel(s, "p2")
        await s.commit()
        msg = _Msg()
        state = FSMContext(storage=MemoryStorage(),
                           key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        await handlers._do_owner_menu("newadmin", msg, state, s)
        assert len(msg.sent) == 1
        kb = msg.sent[0][1]["reply_markup"]
        data = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert data[:2] == ["napanel:1", "napanel:2"] and data[-1] == "cancel"
        assert await state.get_state() is None  # the panel comes first, the name after
    _run(body)


# ---------------- the API client's request shape ----------------

@pytest.mark.parametrize("generation", [12, 14])
def test_create_admin_sends_every_field_the_panel_requires(monkeypatch, generation):
    """`AdminSchema` marks name/mode/can_add_admin/lang REQUIRED — a body missing any is rejected."""
    captured: dict = {}

    import json
    reads = []
    def handle(request):
        assert request.headers["Hiddify-API-Key"] == OWNER_UUID
        if request.method == "POST":
            captured.update(url=str(request.url), headers=request.headers, body=json.loads(request.content))
            return httpx.Response(200, json={"uuid": "ignored-response-uuid"})
        reads.append(request.url.path)
        if request.url.path.endswith(f"/{OWNER_UUID}/"):
            owner = {"uuid": OWNER_UUID, "mode": "super_admin"}
            if generation == 14:
                owner["capabilities"] = None  # field presence, not truthiness
            return httpx.Response(200, json=owner)
        return httpx.Response(200, json=captured["body"])
    transport(monkeypatch, handle)
    panel = api_panel()
    panel.owner_uuid = OWNER_UUID
    uid = asyncio.run(admin_api.AdminApiClient().create_admin(
        panel, name="n", api_key=OWNER_UUID, parent_admin_uuid=OWNER_UUID,
        admin_uuid="AAAA-BBBB",
    ))
    body = captured["body"]
    assert captured["url"] == f"{panel.admin_api_base}/admin_user/"
    assert captured["headers"]["Hiddify-API-Key"] == OWNER_UUID
    assert body["mode"] == "agent" and body["can_add_admin"] is False and body["lang"] == "fa"
    assert body["name"] == "n" and body["parent_admin_uuid"] == OWNER_UUID
    assert body["uuid"] == "aaaa-bbbb"  # lowercased: `_norm_uuid` lowercases everything sync sees
    assert "password" not in body and "new_password" not in body
    assert uid == "aaaa-bbbb"  # requested identity, verified by GET; never trust a POST uuid
    assert reads == [f"/secret/api/v2/admin/admin_user/{OWNER_UUID}/",
                     "/secret/api/v2/admin/admin_user/aaaa-bbbb/"]
    if generation == 14:
        assert body["capabilities"] == AGENT_CAPABILITIES
    else:
        # v12 AdminSchema rejects unknown fields; do not send v14-only capabilities.
        assert set(body) == {"uuid", "name", "mode", "can_add_admin", "lang",
                             "parent_admin_uuid", "max_users", "max_active_users"}


def test_create_admin_maps_the_duplicate_uuid_answer(monkeypatch):
    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, json={"uuid": OWNER_UUID, "mode": "super_admin"})
        return httpx.Response(400, text="The admin exists")
    transport(monkeypatch, handle)
    panel = api_panel()
    panel.owner_uuid = OWNER_UUID
    with pytest.raises(AdminExistsError):
        asyncio.run(admin_api.AdminApiClient().create_admin(
            panel, name="n", api_key=OWNER_UUID, parent_admin_uuid=OWNER_UUID))


def _v14_policy(monkeypatch):
    """Execute the actual panel policy fixture, without importing the running panel/app."""
    class AdminMode(StrEnum):
        agent = "agent"
        admin = "admin"
        super_admin = "super_admin"
    monkeypatch.setitem(sys.modules, "hiddifypanel.models.admin", SimpleNamespace(AdminMode=AdminMode))
    return SimpleNamespace(**runpy.run_path(str(Path(__file__).parent / "fixtures/hiddify14_admin_capabilities.py")))


def test_real_v14_merge_grants_agent_customization_without_management(monkeypatch):
    caps = _v14_policy(monkeypatch)
    owner = SimpleNamespace(mode="super_admin", capabilities=None)
    # This is how validate_admin_write_payload expands the OLD request (can_add_admin=False).
    old = caps.merge_for_actor(owner, {"add_sub_admins": False}, creating=True)
    assert not any(old.values())
    effective = caps.merge_for_actor(owner, AGENT_CAPABILITIES, creating=True)
    assert effective == caps.defaults_for_mode("agent", can_add_admin=False)
    agent = SimpleNamespace(mode="agent", capabilities=effective, can_add_admin=False)
    assert caps.has(agent, "modify_user_extra")
    assert caps.has(agent, "add_extra_sublinks")
    assert not any(caps.has(agent, k) for k in (
        "add_sub_admins", "override_preferred_outbound", "modify_domains", "modify_outbounds"))


@pytest.mark.parametrize("corruption", [
    "modify_user_extra", "add_extra_sublinks", "add_sub_admins", "modify_domains",
    "modify_outbounds", "override_preferred_outbound", "mode", "can_add_admin", "uuid",
    "parent_admin_uuid", "missing_caps", "read_error", "max_users",
])
def test_successful_post_is_not_enough_without_correct_readback(monkeypatch, corruption):
    import json
    state = {}
    def handle(request):
        if request.url.path.endswith("/admin_user/owner/"):
            return httpx.Response(200, json={"uuid": "owner", "mode": "super_admin", "capabilities": None})
        if request.method == "POST":
            state.update(json.loads(request.content))
            return httpx.Response(200, json=state)
        if corruption == "read_error":
            return httpx.Response(503)
        row = {**state, "capabilities": dict(state["capabilities"])}
        if corruption in AGENT_CAPABILITIES:
            row["capabilities"][corruption] = not AGENT_CAPABILITIES[corruption]
        elif corruption == "missing_caps":
            row.pop("capabilities")
        else:
            row[corruption] = {"mode": "super_admin", "can_add_admin": True,
                               "uuid": "another", "parent_admin_uuid": "another", "max_users": 999}[corruption]
        return httpx.Response(200, json=row)
    transport(monkeypatch, handle)
    with pytest.raises(AdminVerificationError):
        asyncio.run(admin_api.AdminApiClient().create_admin(
            api_panel(), name="n", api_key="owner", parent_admin_uuid="owner", admin_uuid="new"))


@pytest.mark.parametrize("response", [401, 403, 404, 500, "wrong_actor", "invalid_contract"])
def test_unverified_owner_never_falls_back_to_legacy_creation(monkeypatch, response):
    def handle(request):
        assert request.method == "GET"  # no POST on auth, transport, or schema failure
        if isinstance(response, int):
            return httpx.Response(response)
        actor = {"uuid": "owner", "mode": "agent" if response == "wrong_actor" else "super_admin"}
        if response == "invalid_contract":
            actor["capabilities"] = []
        return httpx.Response(200, json=actor)
    transport(monkeypatch, handle)
    with pytest.raises(RuntimeError):
        asyncio.run(admin_api.AdminApiClient().create_admin(
            api_panel(), name="n", api_key="owner", parent_admin_uuid="owner"))


def test_unverified_creation_never_registers_or_returns_a_login_link(monkeypatch, caplog):
    async def unverified(*args, **kwargs):
        raise AdminVerificationError("readback mismatch")
    monkeypatch.setattr(admin_api.AdminApiClient, "create_admin", unverified)
    async def body(s):
        panel = _panel(s)
        await s.commit()
        result = await resellercreate.create(s, panel, name="n")
        assert not result.ok and result.reason == "verification" and result.link is None
        assert (await s.scalars(select(Reseller))).all() == []
    _run(body)
    assert OWNER_UUID not in caplog.text


@pytest.mark.parametrize("parent_exists", [False, True])
def test_contract_probe_can_select_only_a_verified_existing_child_parent(monkeypatch, parent_exists):
    import json
    state = {}
    def handle(request):
        if request.url.path.endswith("/owner/"):
            return httpx.Response(200, json={"uuid": "owner", "mode": "super_admin", "capabilities": None})
        if request.url.path.endswith("/parent/"):
            return httpx.Response(200, json={"uuid": "parent", "mode": "agent"}) if parent_exists else httpx.Response(404)
        if request.method == "POST":
            assert parent_exists
            state.update(json.loads(request.content))
            return httpx.Response(200, json=state)
        return httpx.Response(200, json=state)
    transport(monkeypatch, handle)
    call = admin_api.AdminApiClient().create_admin(
        api_panel(), name="n", api_key="owner", parent_admin_uuid="parent", admin_uuid="new")
    if parent_exists:
        assert asyncio.run(call) == "new"
        assert state["parent_admin_uuid"] == "parent"
    else:
        with pytest.raises(RuntimeError, match="parent"):
            asyncio.run(call)
        assert not state
