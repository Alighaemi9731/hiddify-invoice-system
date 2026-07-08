"""H07 — reseller PATCH null-vs-absent semantics.

The edit dialog sends an explicit JSON null to CLEAR a per-reseller override back to the
global default. The endpoint must gate on field PRESENCE (model_fields_set), not `is not
None` — otherwise an explicit null is indistinguishable from an absent field and silently
ignored (a stale override survives while the UI shows success).
"""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./data/resellerpatch.db")
os.environ.setdefault("SECRET_KEY", "k")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.api import resellers as resellers_api  # noqa: E402
from app.models import Panel, Reseller  # noqa: E402
from app.schemas.reseller import ResellerUpdate  # noqa: E402


def _run(coro_fn, tmp_path, name):
    async def go():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/name}")
        from app.core.db import Base
        async with engine.begin() as c:
            await c.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with Session() as s:
                await coro_fn(s)
        finally:
            await engine.dispose()
    asyncio.run(go())


async def _seed(s, **kw):
    p = Panel(id=1, key="p", host="h", proxy_path_enc="x", owner_uuid="o")
    s.add(p)
    await s.flush()
    r = Reseller(panel_id=1, admin_uuid="a", name="R", **kw)
    s.add(r)
    await s.commit()
    return r


def test_null_clears_price_and_fee_override(tmp_path):
    async def body(s):
        r = await _seed(s, price_per_gb=5000, storefront_monthly_fee_toman=300_000,
                        min_sale_toman=500_000)
        # Explicit nulls — «خالی = پیش‌فرض».
        body_json = ResellerUpdate.model_validate(
            {"price_per_gb": None, "storefront_monthly_fee_toman": None,
             "min_sale_toman": None})
        await resellers_api.update_reseller(r.id, body_json, session=s)
        await s.refresh(r)
        assert r.price_per_gb is None                  # cleared to global default
        assert r.storefront_monthly_fee_toman is None  # cleared
        assert r.min_sale_toman is None                # cleared (fails before: stayed 500000)

    _run(body, tmp_path, "p1.db")


def test_absent_fields_leave_overrides_untouched(tmp_path):
    async def body(s):
        r = await _seed(s, price_per_gb=5000, min_sale_toman=500_000)
        # Only exclude_from_billing is sent; the money overrides are ABSENT → untouched.
        body_json = ResellerUpdate.model_validate({"exclude_from_billing": True})
        await resellers_api.update_reseller(r.id, body_json, session=s)
        await s.refresh(r)
        assert r.exclude_from_billing is True
        assert r.price_per_gb == 5000
        assert r.min_sale_toman == 500_000

    _run(body, tmp_path, "p2.db")


def test_zero_min_sale_kept_as_no_floor(tmp_path):
    async def body(s):
        r = await _seed(s, min_sale_toman=500_000)
        body_json = ResellerUpdate.model_validate({"min_sale_toman": 0})
        await resellers_api.update_reseller(r.id, body_json, session=s)
        await s.refresh(r)
        assert r.min_sale_toman == 0     # explicit "no floor", NOT coerced to None/default

    _run(body, tmp_path, "p3.db")
