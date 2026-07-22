"""Parity: `bundle_extra_for_bundles` (Wave 6 batch) must produce EXACTLY what the
trusted per-bundle `bundle_extra` produces, bundle by bundle — money math is at stake.

Covers: empty bundle, normal usage, high usage, decimal precision, multiple bundles,
excluded (trial) uuids, users with logged events (renewal enumeration), rows belonging
to no bundle, and an empty panel.
"""
import asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models import UsageMeter, UsageMeterEvent
from app.services import metering


async def _mk(tmp_path, name):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / name}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _meter(panel_id, user, admin, period, *, quota=0.0, consumed=0.0, overage=0.0,
           edit_renewal=0.0, renew_used=0.0, resets=0):
    return UsageMeter(
        panel_id=panel_id, user_uuid=user, added_by_uuid=admin, period_label=period,
        name=f"u-{user}", quota_added_gb=quota, consumed_gb=consumed, overage_gb=overage,
        edit_renewal_gb=edit_renewal, renew_used_gb=renew_used, reset_count=resets,
    )


def test_batched_bundle_extra_matches_per_bundle(tmp_path):
    async def run():
        engine, Session = await _mk(tmp_path, "parity.db")
        try:
            async with Session() as s:
                P, LBL = 1, "2026-07"
                bundles = [
                    {"a1", "a1s"},   # two-admin subtree, mixed shapes
                    {"a2"},          # decimals + high usage
                    set(),           # empty bundle
                    {"a3"},          # only excluded/trial users
                    {"a4"},          # no meter rows at all
                ]
                trial = {"u-trial-1"}
                s.add_all([
                    _meter(P, "u1", "a1", LBL, quota=10, consumed=14.5, overage=4.5, resets=2),
                    _meter(P, "u2", "a1s", LBL, quota=5, edit_renewal=7.25),
                    _meter(P, "u3", "a1", LBL, quota=0.5, consumed=0.4),      # under threshold
                    _meter(P, "u4", "a2", LBL, quota=1000, consumed=1500.333,
                           overage=500.333, resets=9),
                    _meter(P, "u5", "a2", LBL, quota=1.5, edit_renewal=0.001),
                    _meter(P, "u-trial-1", "a3", LBL, quota=20, overage=20),  # excluded
                    _meter(P, "u-other", "zz", LBL, quota=50, overage=50),    # no bundle
                    _meter(P, "u1", "a1", "2026-06", quota=9, overage=9),     # other period
                ])
                s.add_all([
                    UsageMeterEvent(panel_id=P, user_uuid="u2", period_label=LBL,
                                    kind="edit_topup", gb=7.25),
                    UsageMeterEvent(panel_id=P, user_uuid="u1", period_label=LBL,
                                    kind="renewal", gb=4.5),
                ])
                await s.commit()

                free_threshold = 1.0
                batched = await metering.bundle_extra_for_bundles(
                    s, P, bundles, LBL, free_threshold, exclude_user_uuids=trial,
                )
                assert len(batched) == len(bundles)
                for i, b in enumerate(bundles):
                    single = await metering.bundle_extra(
                        s, P, b, LBL, free_threshold, exclude_user_uuids=trial,
                    ) if b else {"gb": 0.0, "lines": [], "abnormal": []}
                    assert batched[i] == single, f"bundle {i} diverged: {batched[i]} != {single}"

                # sanity on the shapes the money path consumes
                assert batched[0]["gb"] > 0            # real extras detected
                assert batched[3]["gb"] == 0.0         # trial fully excluded
                assert batched[4] == {"gb": 0.0, "lines": [], "abnormal": []}
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_batched_empty_panel_and_disabled(tmp_path, monkeypatch):
    async def run():
        engine, Session = await _mk(tmp_path, "parity2.db")
        try:
            async with Session() as s:
                out = await metering.bundle_extra_for_bundles(s, 1, [], "2026-07", 1.0)
                assert out == []
                out = await metering.bundle_extra_for_bundles(
                    s, 1, [{"a"}, set()], "2026-07", 1.0
                )
                assert out == [{"gb": 0.0, "lines": [], "abnormal": []}] * 2

                async def _off(_s):
                    return False
                monkeypatch.setattr(metering, "is_enabled", _off)
                out = await metering.bundle_extra_for_bundles(
                    s, 1, [{"a"}], "2026-07", 1.0
                )
                assert out == [{"gb": 0.0, "lines": [], "abnormal": []}]
        finally:
            await engine.dispose()
    asyncio.run(run())
