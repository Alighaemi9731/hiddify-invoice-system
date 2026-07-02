"""Router inventory snapshot (refactor guard for the app.bot.handlers package split).

aiogram dispatches first-match in REGISTRATION order, so the exact ordered list of
handlers per observer is behavior. This test regenerates the inventory from the live
router and asserts it equals the checked-in fixture captured from the original
monolithic ``app/bot/handlers.py`` — any reordering, addition, or loss of a handler
(or router-level middleware) during/after the package split fails loudly.
"""
from __future__ import annotations

import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "bot_router_inventory.json"


def build_inventory() -> dict:
    """For each router observer (message, callback_query, ...): the ORDERED handler
    function names plus the registered outer/inner middleware names."""
    from app.bot.handlers import router

    inventory: dict[str, dict[str, list[str]]] = {}
    for name, observer in router.observers.items():
        handlers = [h.callback.__name__ for h in getattr(observer, "handlers", [])]
        outer = [
            getattr(m, "__name__", type(m).__name__)
            for m in getattr(observer, "outer_middleware", [])
        ]
        inner = [
            getattr(m, "__name__", type(m).__name__)
            for m in getattr(observer, "middleware", [])
        ]
        if handlers or outer or inner:
            inventory[name] = {
                "handlers": handlers,
                "outer_middleware": outer,
                "middleware": inner,
            }
    return inventory


def test_router_inventory_matches_fixture() -> None:
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert build_inventory() == expected
