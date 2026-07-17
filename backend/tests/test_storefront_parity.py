"""Plan 007 parity contract: every storefront admin-bot capability maps to a portal route + a shared
`storefront_admin`/`storefront_customers`/`storefront_reporting`/`storefront_provision` command, and
no portal MUTATION handler bypasses that shared layer with a direct session write. The fixture
`fixtures/storefront_admin_parity.json` is the machine-readable inventory; this test fails in BOTH
directions (a new menu action without a row, or an orphan row) and enforces the no-bypass rule so the
Telegram bot can be simplified without losing any owner capability."""
from __future__ import annotations

import ast
import asyncio
import importlib
import json
import pathlib

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.storefront import keyboards as sfkb
from app.core.db import Base, get_session
from app.core.portal_auth import ResellerContext, get_current_reseller
from app.main import app
from app.models import Panel, Reseller, StorefrontBot

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "storefront_admin_parity.json"
DATA = json.loads(FIXTURE.read_text("utf-8"))
ROWS = DATA["rows"]
REQUIRED = {"menu_key", "telegram_label", "portal_route", "api_operation", "shared_symbol",
            "owner_mode", "coadmin_fallback", "test_id"}
_SHARED_MODULES = {"storefront_admin", "storefront_customers", "storefront_reporting",
                   "storefront_provision"}
_SESSION_WRITERS = {"add", "delete", "merge", "execute", "flush", "commit"}


def test_top_level_menu_keys_match_admin_menu_both_directions():
    top = {r["menu_key"] for r in ROWS if "." not in r["menu_key"] and r["owner_mode"] != "bot_native"}
    assert top == set(sfkb.ADMIN_LABEL_TO_ACTION.values())   # fails if a menu action lacks a row OR orphan row
    fixture_labels = {r["telegram_label"] for r in ROWS if r["owner_mode"] != "bot_native"}
    assert {lbl for lbl, _ in sfkb.ADMIN_MENU} <= fixture_labels


def test_rows_have_required_fields_and_unique_keys():
    menu_keys, test_ids = [], []
    for r in ROWS:
        assert REQUIRED <= set(r), f"missing fields in {r.get('menu_key')}"
        if r["owner_mode"] == "bot_native":
            assert r.get("gap_reason"), f"gap row {r['menu_key']} needs gap_reason"
        menu_keys.append(r["menu_key"])
        test_ids.append(r["test_id"])
    assert len(menu_keys) == len(set(menu_keys)), "duplicate menu_key"
    assert len(test_ids) == len(set(test_ids)), "duplicate test_id"


def test_every_shared_symbol_is_importable():
    for r in ROWS:
        sym = r["shared_symbol"]
        if not sym:
            continue
        mod, attr = sym.rsplit(".", 1)
        assert hasattr(importlib.import_module(mod), attr), f"unresolved shared_symbol {sym}"


def test_every_api_operation_is_a_registered_route():
    registered = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", set()) or set():
            registered.add(f"{method} {path}")
    for r in ROWS:
        op = r["api_operation"]
        if not op:
            continue
        assert op in registered, f"{r['menu_key']} declares an unregistered route: {op}"


def test_no_portal_mutation_handler_bypasses_the_shared_command_layer():
    """Static AST guard: every @router.post/patch/put/delete handler body may call the shared command
    modules (or local helpers), but NEVER `session.add/execute/delete/flush/commit` directly — a portal
    mutation must go through the audited/idempotent storefront_admin layer, not raw ORM writes."""
    src = (pathlib.Path("app/api/portal_storefront.py")).read_text("utf-8")
    tree = ast.parse(src)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        methods = set()
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                if isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router":
                    methods.add(dec.func.attr)
        if not (methods & {"post", "patch", "put", "delete"}):
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                if (isinstance(call.func.value, ast.Name)
                        and call.func.value.id in {"session", "s", "db"}
                        and call.func.attr in _SESSION_WRITERS):
                    violations.append(f"{node.name}: session.{call.func.attr}(...)")
    assert not violations, f"portal mutation handlers bypass the shared layer: {violations}"


def test_legacy_callback_removal_pin_is_a_future_version():
    pin = DATA["legacy_owner_callbacks_remove_not_before"]
    assert isinstance(pin, str) and pin.startswith("v")
    parts = pin[1:].split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)
    # Must be strictly after the current release (v1.88.0).
    assert tuple(int(p) for p in parts) > (1, 88, 0)


# ── auth-contract harness: foreign shop -> 404 (reads); missing Idempotency-Key -> 422 (mutations) ──

def _run(body):  # noqa: ANN001, ANN202
    async def go():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as session:
                await body(session)
        finally:
            app.dependency_overrides.clear()
            await engine.dispose()

    asyncio.run(go())


async def _seed(session):  # noqa: ANN001, ANN202
    panel = Panel(key="p1", name="P1", host="p1.invalid", proxy_path_enc="x", owner_uuid="o")
    session.add(panel)
    await session.flush()
    owner = Reseller(panel_id=panel.id, admin_uuid="own", name="Own", bot_chat_id=111,
                     storefront_enabled=True)
    foreign = Reseller(panel_id=panel.id, admin_uuid="fgn", name="Fgn", bot_chat_id=222,
                       storefront_enabled=True)
    session.add_all([owner, foreign])
    await session.flush()
    shop = StorefrontBot(reseller_id=owner.id, panel_id=panel.id, bot_token_enc="t", config_version=1)
    other = StorefrontBot(reseller_id=foreign.id, panel_id=panel.id, bot_token_enc="t2",
                          config_version=1)
    session.add_all([shop, other])
    await session.flush()
    await session.commit()
    return owner, shop, other


def _fill(path: str, shop_id: int) -> str:
    return (path.replace("{shop_id}", str(shop_id)).replace("{plan_id}", "1")
            .replace("{customer_id}", "1").replace("{code_id}", "1").replace("{order_id}", "1")
            .replace("{txn_id}", "1").replace("{job_id}", "1").replace("{telegram_id}", "1"))


def test_api_operations_enforce_tenant_and_command_contract():
    async def body(session):  # noqa: ANN001
        owner, shop, other = await _seed(session)

        async def session_override():
            yield session

        async def context_override():
            return ResellerContext(chat_id=111, resellers=[owner])

        app.dependency_overrides[get_session] = session_override
        app.dependency_overrides[get_current_reseller] = context_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            seen_methods: set[str] = set()
            for r in ROWS:
                op = r["api_operation"]
                if not op:
                    continue
                method, path = op.split(" ", 1)
                # A GET read on a FOREIGN shop → 404 (tenant choke point, no existence leak).
                if method == "GET" and "/proof" not in path:
                    url = _fill(path, other.id)
                    resp = await client.get(url, params={"from": "2026-07-01", "to": "2026-07-31"})
                    assert resp.status_code == 404, f"{op} foreign shop should 404, got {resp.status_code}"
                # A mutation missing the required Idempotency-Key header → 422 (once per method).
                elif method in ("POST", "PATCH", "PUT", "DELETE") and method not in seen_methods:
                    seen_methods.add(method)
                    url = _fill(path, shop.id)
                    resp = await client.request(method, url, json={})
                    assert resp.status_code == 422, f"{op} missing Idempotency-Key should 422, got {resp.status_code}"

    _run(body)
