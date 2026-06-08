"""The overage tolerance: a small per-user overage (xray soft-cutoff, a few hundred MB after
the quota is hit) is NOT billed as abuse, while a real reset-abuse overage (many GB) still is."""
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models import UsageMeter
from app.services import metering

R = "reseller-uuid"


@pytest.mark.asyncio
async def test_overage_tolerance_ignores_soft_cutoff(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'m.db'}")
    async with engine.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as s:
        s.add_all([
            # xray soft-cutoff: 0.13 GB over → below the 0.5 default tolerance → NOT billed.
            UsageMeter(panel_id=1, user_uuid="soft", period_label="2026-06", added_by_uuid=R,
                       name="soft", overage_gb=0.13, edit_renewal_gb=0),
            # real daily-reset abuse: 10 GB over → billed (minus the 0.5 tolerance).
            UsageMeter(panel_id=1, user_uuid="abuse", period_label="2026-06", added_by_uuid=R,
                       name="abuse", overage_gb=10.0, edit_renewal_gb=0),
        ])
        await s.commit()

        res = await metering.bundle_extra(s, 1, {R}, "2026-06", free_threshold_gb=1.0)

        billed = {ln["user_uuid"]: ln["usage_gb"] for ln in res["lines"]}
        assert "soft" not in billed                       # soft-cutoff ignored entirely
        assert abs(billed["abuse"] - 9.5) < 1e-6          # 10 − 0.5 tolerance
        assert abs(res["gb"] - 9.5) < 1e-6                # only the real abuse is in the total

    await engine.dispose()
