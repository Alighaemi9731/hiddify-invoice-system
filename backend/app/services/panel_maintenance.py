"""Per-panel maintenance, drained against mutations across API, bot and scheduler.

Postgres shared session advisory locks cover the remote operation. Entering maintenance
uses the matching exclusive transaction lock, so success means existing writers drained.
No polling/sleep and no global maintenance: unrelated panels keep working.
"""
from __future__ import annotations

import asyncio
import inspect
import weakref
from collections.abc import Callable, Coroutine
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    async_object_session,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.db import SessionLocal
from app.models import (
    Panel,
    Reseller,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
)

MESSAGE = "این پنل در حال به‌روزرسانی است؛ لطفاً بعداً تلاش کنید."
_NAMESPACE = 0x50414E4C
_active: ContextVar[frozenset[tuple[int, int]]] = ContextVar("panel_operations", default=frozenset())


class PanelMaintenanceError(RuntimeError):
    def __init__(self):
        super().__init__(MESSAGE)


class _PanelLease:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.writers = 0
        self.guard: AsyncConnection | None = None
        self.engine = None

    async def enter(self, bind, panel_id: int) -> None:  # noqa: ANN001
        async with self.lock:
            if self.writers == 0:
                # A separate connection avoids exhausting the business pool with writers
                # waiting to acquire their wallet/order transactions. One lease per panel,
                # per process, is shared by all its concurrent operations.
                self.engine = create_async_engine(bind.url, poolclass=NullPool)
                # Hold the connection itself: a Session.commit() would return it to
                # NullPool and release the session-level advisory lock mid-operation.
                try:
                    self.guard = await self.engine.connect()
                    await self.guard.execute(text("SELECT pg_advisory_lock_shared(:ns, :id)"),
                                             {"ns": _NAMESPACE, "id": panel_id})
                    maintenance = await self.guard.scalar(select(Panel.maintenance).where(Panel.id == panel_id))
                    if maintenance is not False:
                        raise PanelMaintenanceError()
                    await self.guard.commit()  # Advisory lease survives without an idle transaction.
                except BaseException:
                    await self._close(panel_id)
                    raise
            self.writers += 1

    async def _close(self, panel_id: int) -> None:
        guard, engine = self.guard, self.engine
        self.guard = self.engine = None
        try:
            if guard is not None:
                await guard.execute(text("SELECT pg_advisory_unlock_shared(:ns, :id)"),
                                    {"ns": _NAMESPACE, "id": panel_id})
        finally:
            try:
                if guard is not None:
                    await guard.close()
            finally:
                if engine is not None:
                    await engine.dispose()

    async def leave(self, panel_id: int) -> None:
        async with self.lock:
            self.writers -= 1
            if self.writers == 0:
                await self._close(panel_id)


_leases: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.WeakValueDictionary[tuple[int, int], _PanelLease]
] = weakref.WeakKeyDictionary()


@asynccontextmanager
async def panel_operation(panel, session=None):  # noqa: ANN001
    if panel is None:
        yield
        return
    if getattr(panel, "maintenance", False):
        raise PanelMaintenanceError()
    panel_id = getattr(panel, "id", None)
    if not isinstance(panel_id, int):
        yield
        return
    operation_key = (panel_id, id(asyncio.current_task()))
    if operation_key in _active.get():
        yield
        return
    attached = session or async_object_session(panel)
    bind = attached.bind if attached else SessionLocal.kw.get("bind")
    if bind is None or bind.dialect.name != "postgresql":
        yield
        return
    loop = asyncio.get_running_loop()
    leases = _leases.setdefault(loop, weakref.WeakValueDictionary())
    lease_key = (id(bind), panel_id)
    lease = leases.get(lease_key)
    if lease is None:
        lease = _PanelLease()
        leases[lease_key] = lease
    await lease.enter(bind, panel_id)
    token = _active.set(_active.get() | {operation_key})
    try:
        yield
    finally:
        _active.reset(token)
        await lease.leave(panel_id)


def writable_panel[**P, T](
    function: Callable[P, Coroutine[Any, Any, T]],
) -> Callable[P, Coroutine[Any, Any, T]]:
    @wraps(function)
    async def guarded(*args: P.args, **kwargs: P.kwargs) -> T:
        panel = args[1] if len(args) > 1 else kwargs.get("panel")
        async with panel_operation(panel):
            return await function(*args, **kwargs)
    return guarded


async def panel_for_order(session, order_id: int):  # noqa: ANN001
    order = await session.get(StorefrontOrder, order_id)
    if order is None:
        return None
    if order.panel_id is not None:
        return await session.get(Panel, order.panel_id)
    customer = await session.get(StorefrontCustomer, order.customer_id)
    sf = await session.get(StorefrontBot, customer.storefront_bot_id) if customer else None
    reseller = await session.get(Reseller, sf.reseller_id) if sf else None
    return await session.get(Panel, reseller.panel_id) if reseller else None


def storefront_write(result_type: Callable[..., Any]):
    """Drain complete purchase/trial/renew/delete transactions, including the wallet phase."""
    def decorate(function):
        signature = inspect.signature(function)

        @wraps(function)
        async def guarded(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            values = bound.arguments
            factory = values.get("session_factory", SessionLocal)
            async with factory() as session:
                sf = await session.get(StorefrontBot, values["sf_id"]) if "sf_id" in values else None
                if sf is None and "order_id" in values:
                    order = await session.get(StorefrontOrder, values["order_id"])
                    customer = await session.get(StorefrontCustomer, order.customer_id) if order else None
                    sf = await session.get(StorefrontBot, customer.storefront_bot_id) if customer else None
                expected_sf_id = values.get("expected_sf_id")
                if expected_sf_id is not None and (sf is None or sf.id != expected_sf_id):
                    return result_type(False, reason="not_found")
                reseller = await session.get(Reseller, sf.reseller_id) if sf else None
                panel = await session.get(Panel, reseller.panel_id) if reseller else None
                # Detach before calling the existing service; the guard owns its own connection.
                if panel is not None:
                    session.expunge(panel)
                bind = session.bind
            try:
                async with async_sessionmaker(bind, expire_on_commit=False)() as guard_session:
                    async with panel_operation(panel, guard_session):
                        return await function(*args, **kwargs)
            except PanelMaintenanceError:
                return result_type(False, reason="maintenance", message=MESSAGE)
        return guarded
    return decorate


async def set_maintenance(session, panel: Panel, enabled: bool) -> None:  # noqa: ANN001
    if session.bind.dialect.name == "postgresql":
        acquired = await session.scalar(
            text("SELECT pg_try_advisory_xact_lock(:ns, :id)"),
            {"ns": _NAMESPACE, "id": panel.id},
        )
        if not acquired:
            raise RuntimeError("این پنل عملیات در حال اجرا دارد؛ پس از پایان آن دوباره تلاش کنید.")
    panel.maintenance = enabled
    await session.commit()
