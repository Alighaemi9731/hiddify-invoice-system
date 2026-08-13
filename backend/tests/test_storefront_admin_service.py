"""Release B core contracts: CAS, idempotency, audit and shared absolute commands."""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.db import Base
from app.models import (
    Panel,
    Reseller,
    StorefrontApiCommand,
    StorefrontAuditEvent,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontPlan,
    StorefrontWalletTxn,
)
from app.services import (
    settings_service,
    storefront,
    storefront_admin,
    storefront_audit,
)
from tests.pg_barrier import make_engine, requires_pg


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
            await engine.dispose()

    asyncio.run(go())


async def _seed(session):  # noqa: ANN001, ANN202
    # Pin the global price the below-cost floor is computed from, so the shipped default
    # can move without rewriting the plan prices below.
    await settings_service.set_value(session, "default_price_per_gb", 1000)
    # Same reasoning for the trial ceiling: these tests are about the shared command layer, not
    # about the owner's giveaway budget. The cap is covered by test_storefront_trial.py.
    await settings_service.set_value(session, "storefront_trial_max_gb", 100)
    panel = Panel(key="p1", host="p1.invalid", proxy_path_enc="panel-secret", owner_uuid="owner")
    session.add(panel)
    await session.flush()
    owner = Reseller(
        panel_id=panel.id, admin_uuid="r1", name="Owner", bot_chat_id=111,
        storefront_enabled=True,
    )
    foreign = Reseller(
        panel_id=panel.id, admin_uuid="r2", name="Foreign", bot_chat_id=222,
        storefront_enabled=True,
    )
    session.add_all([owner, foreign])
    await session.flush()
    shop = StorefrontBot(
        reseller_id=owner.id, panel_id=panel.id, bot_token_enc="encrypted-token",
        bot_username="shop_bot", config_version=1, pay_card_enabled=False,
    )
    other = StorefrontBot(
        reseller_id=foreign.id, panel_id=panel.id, bot_token_enc="foreign-token",
        bot_username="other_bot", config_version=1, pay_card_enabled=False,
    )
    session.add_all([shop, other])
    await session.commit()
    return owner, foreign, shop, other


def _ctx(version: int, key: str = "key-1", *, actor: int = 111, source="portal"):  # noqa: ANN001, ANN202
    return storefront_admin.CommandContext(
        actor_telegram_id=actor, actor_role="owner", source=source,
        idempotency_key=key, expected_version=version,
    )


def test_canonical_hash_redaction_and_response_cap():
    assert storefront_audit.canonical_request_hash({"b": 2, "a": 1}) == (
        storefront_audit.canonical_request_hash({"a": 1, "b": 2})
    )
    safe = storefront_audit.safe_cached_response({
        "bot_token": "secret", "nested": {"sub_link": "https://secret"},
    })
    assert safe == {"bot_token": "[REDACTED]", "nested": {"sub_link": "[REDACTED]"}}
    large = storefront_audit.safe_cached_response({"text": "الف" * 20_000})
    assert isinstance(large, dict) and large["truncated"] is True
    assert len(storefront_audit.canonical_json(large).encode()) <= storefront_audit.MAX_RESPONSE_BYTES


def test_command_lease_replay_conflict_and_external_unknown():
    async def body(session):  # noqa: ANN001
        _owner, _foreign, shop, _other = await _seed(session)
        now = dt.datetime(2026, 7, 16, tzinfo=dt.timezone.utc)
        first = await storefront_audit.claim_command(
            session, shop_id=shop.id, actor_telegram_id=111, idempotency_key="a",
            action="settings.trial", request={"enabled": True}, now=now,
        )
        assert first.outcome == "claimed" and first.command.attempt_count == 1
        busy = await storefront_audit.claim_command(
            session, shop_id=shop.id, actor_telegram_id=111, idempotency_key="a",
            action="settings.trial", request={"enabled": True}, now=now,
        )
        assert busy.outcome == "in_flight"
        await storefront_audit.finalize_command(
            session, first.command, succeeded=True, response_status=200,
            response_body={"bot_token": "must-not-persist", "ok": True},
        )
        replay = await storefront_audit.claim_command(
            session, shop_id=shop.id, actor_telegram_id=111, idempotency_key="a",
            action="settings.trial", request={"enabled": True}, now=now,
        )
        assert replay.outcome == "replay"
        assert replay.command.response_body == {"bot_token": "[REDACTED]", "ok": True}
        conflict = await storefront_audit.claim_command(
            session, shop_id=shop.id, actor_telegram_id=111, idempotency_key="a",
            action="settings.trial", request={"enabled": False}, now=now,
        )
        assert conflict.outcome == "conflict"

        db_claim = await storefront_audit.claim_command(
            session, shop_id=shop.id, actor_telegram_id=111, idempotency_key="db",
            action="plan.create", request={}, now=now,
        )
        reclaimed = await storefront_audit.claim_command(
            session, shop_id=shop.id, actor_telegram_id=111, idempotency_key="db",
            action="plan.create", request={}, now=now + dt.timedelta(seconds=61),
        )
        assert reclaimed.outcome == "claimed" and db_claim.command.attempt_count == 2

        await storefront_audit.claim_command(
            session, shop_id=shop.id, actor_telegram_id=111, idempotency_key="io",
            action="channel.verify", request={}, external_io=True, now=now,
        )
        unknown = await storefront_audit.claim_command(
            session, shop_id=shop.id, actor_telegram_id=111, idempotency_key="io",
            action="channel.verify", request={}, external_io=True,
            now=now + dt.timedelta(seconds=61),
        )
        assert unknown.outcome == "unknown" and unknown.command.status == "unknown"

    _run(body)


def test_command_retention_marks_expired_external_unknown_and_prunes_terminal():
    async def body(session):  # noqa: ANN001
        _owner, _foreign, shop, _other = await _seed(session)
        now = dt.datetime(2026, 7, 16, tzinfo=dt.timezone.utc)
        old = now - dt.timedelta(days=31)
        session.add_all([
            StorefrontApiCommand(
                storefront_bot_id=shop.id, actor_telegram_id=111, idempotency_key="old-success",
                action="x", request_hash="a" * 64, status="succeeded", external_io=False,
                created_at=old, updated_at=old),
            StorefrontApiCommand(
                storefront_bot_id=shop.id, actor_telegram_id=111, idempotency_key="old-external",
                action="channel.save", request_hash="b" * 64, status="pending", external_io=True,
                lease_expires_at=old, created_at=old, updated_at=old),
            StorefrontApiCommand(
                storefront_bot_id=shop.id, actor_telegram_id=111, idempotency_key="db-pending",
                action="x", request_hash="c" * 64, status="pending", external_io=False,
                lease_expires_at=old, created_at=old, updated_at=old),
        ])
        await session.commit()
        assert await storefront_audit.prune_commands(session, now=now) == 2
        await session.commit()
        rows = (await session.execute(select(StorefrontApiCommand))).scalars().all()
        assert [(row.idempotency_key, row.status) for row in rows] == [("db-pending", "pending")]

    _run(body)


def test_plan_commands_are_cas_audited_idempotent_and_tenant_safe():
    async def body(session):  # noqa: ANN001
        _owner, _foreign, shop, other = await _seed(session)
        created = await storefront_admin.create_plan(
            session, shop.id, _ctx(1), gb=10, days=30, price_toman=100_000)
        assert created.response_status == 201 and created.config_version == 2
        plan_id = created.body["plan"]["id"]
        assert created.body["plan"]["title"] == ""
        replay = await storefront_admin.create_plan(
            session, shop.id, _ctx(1), gb=10, days=30, price_toman=100_000)
        assert replay.replayed is True and replay.body == created.body
        assert await session.scalar(select(func.count(StorefrontPlan.id))) == 1

        with pytest.raises(storefront_admin.AdminCommandError) as idem:
            await storefront_admin.create_plan(
                session, shop.id, _ctx(1), gb=11, days=30, price_toman=100_000)
        assert idem.value.code == "idempotency_conflict"

        with pytest.raises(storefront_admin.AdminCommandError) as stale:
            await storefront_admin.set_plan_enabled(
                session, shop.id, plan_id, _ctx(1, "stale"), enabled=False)
        assert stale.value.code == "config_conflict" and stale.value.current_version == 2

        toggled = await storefront_admin.set_plan_enabled(
            session, shop.id, plan_id, _ctx(2, "toggle"), enabled=False)
        assert toggled.config_version == 3 and toggled.body["plan"]["enabled"] is False
        with pytest.raises(storefront_admin.AdminCommandError) as foreign_child:
            await storefront_admin.set_plan_enabled(
                session, other.id, plan_id, _ctx(1, "foreign", actor=222), enabled=True)
        assert foreign_child.value.code == "not_found"

        events = (
            await session.execute(
                select(StorefrontAuditEvent).where(StorefrontAuditEvent.storefront_bot_id == shop.id)
            )
        ).scalars().all()
        assert [event.outcome for event in events] == ["succeeded", "conflict", "succeeded"]
        history = await storefront_admin.plan_history(session, shop.id, plan_id, 111, "portal")
        assert {row["action"] for row in history} == {"plan.create", "plan.set_enabled"}

    _run(body)


def test_exact_reorder_validation_and_delete_preserve_audit_history():
    async def body(session):  # noqa: ANN001
        _owner, _foreign, shop, _other = await _seed(session)
        # Prices clear the below-cost floor (gb x default_price_per_gb = 1000/GB) so this test
        # exercises reordering, not the pricing guard.
        one = await storefront_admin.create_plan(
            session, shop.id, _ctx(1, "p1"), gb=1, days=1, price_toman=1_000)
        two = await storefront_admin.create_plan(
            session, shop.id, _ctx(2, "p2"), gb=2, days=2, price_toman=2_000)
        ids = [one.body["plan"]["id"], two.body["plan"]["id"]]
        with pytest.raises(storefront_admin.AdminCommandError):
            await storefront_admin.reorder_plans(
                session, shop.id, _ctx(3, "bad-order"), ordered_ids=[ids[0], ids[0]])
        # Distinct but INCOMPLETE set (a plan omitted) must be refused — never silently drop a plan.
        with pytest.raises(storefront_admin.AdminCommandError) as incomplete:
            await storefront_admin.reorder_plans(
                session, shop.id, _ctx(3, "incomplete"), ordered_ids=[ids[0]])
        assert incomplete.value.code == "validation"
        # Distinct but containing a FOREIGN/unknown id must be refused too.
        with pytest.raises(storefront_admin.AdminCommandError) as foreign:
            await storefront_admin.reorder_plans(
                session, shop.id, _ctx(3, "foreign"), ordered_ids=[ids[0], ids[1], 999_999])
        assert foreign.value.code == "validation"
        # Neither rejected attempt lost or reordered a plan.
        kept = await storefront.list_plans(session, shop.id)
        assert [plan.id for plan in kept] == ids
        ordered = await storefront_admin.reorder_plans(
            session, shop.id, _ctx(3, "order"), ordered_ids=list(reversed(ids)))
        assert ordered.body["ordered_ids"] == list(reversed(ids))
        await storefront_admin.delete_plan(session, shop.id, ids[0], _ctx(4, "delete"))
        history = await storefront_admin.plan_history(session, shop.id, ids[0], 111, "portal")
        assert history[0]["action"] == "plan.delete" and history[0]["before"]["id"] == ids[0]

    _run(body)


def test_settings_managers_channel_and_preview_are_shared_absolute_commands():
    async def body(session):  # noqa: ANN001
        _owner, _foreign, shop, _other = await _seed(session)
        trial = await storefront_admin.update_trial(
            session, shop.id, _ctx(1, "trial"), enabled=False, gb=2, days=3)
        assert trial.body["trial"] == {"enabled": False, "gb": 2, "days": 3}
        messages = await storefront_admin.update_messages(
            session, shop.id, _ctx(2, "messages"), welcome_text="  سلام  ", support_contact=" @x ")
        assert messages.body["messages"]["welcome_text"] == "سلام"
        payment = await storefront_admin.update_payment(
            session, shop.id, _ctx(3, "payment"), card_enabled=True,
            card_number="۶۰۳۷-۹۹۹۹-۹۹۹۹-۹۹۹۹", card_holder="Test Holder",
            usdt_enabled=False, usdt_address=None, ton_enabled=False, ton_address=None,
        )
        assert payment.body["payment_updated"] is True
        assert "card_number" in payment.body["updated_fields"]
        assert (
            await session.execute(
                select(StorefrontAuditEvent).where(StorefrontAuditEvent.action == "settings.payment")
            )
        ).scalar_one().after_json["card_number"] == "[REDACTED]"

        manager = await storefront_admin.add_manager(
            session, shop.id, _ctx(4, "manager"), telegram_id=333)
        assert manager.body["managers"]["co_admin_ids"] == [333]
        with pytest.raises(storefront_admin.AdminCommandError) as coadmin_portal:
            await storefront_admin.list_managers(session, shop.id, 333, "portal")
        assert coadmin_portal.value.code == "not_found"

        verification = await storefront_admin.claim_external_channel(
            session, shop.id, _ctx(5, "channel"), channel_id="-1001234567890")
        assert isinstance(verification, storefront_admin.ChannelVerification)
        pending = await session.get(StorefrontApiCommand, verification.command_id)
        assert pending is not None and pending.status == "pending"  # durable before Telegram I/O
        channel = await storefront_admin.finalize_verified_channel(
            session, verification, _ctx(5, "channel"), verified=True,
            resolved_link="https://t.me/+serverDerived",
        )
        assert channel.body["channel"]["required"] is True
        assert channel.body["channel"]["has_link"] is True
        assert "serverDerived" not in repr(channel.body)
        disabled = await storefront_admin.set_channel_required(
            session, shop.id, _ctx(6, "channel-off"), required=False)
        assert disabled.body["channel"]["required"] is False

        before_customers = await session.scalar(select(func.count(StorefrontCustomer.id)))
        preview = await storefront_admin.customer_preview(session, shop.id, 111, "portal")
        after_customers = await session.scalar(select(func.count(StorefrontCustomer.id)))
        assert before_customers == after_customers == 0
        assert preview["trial"] == {"enabled": False, "gb": 2, "days": 3}
        assert preview["channel_required"] is False

    _run(body)


def test_external_channel_expired_lease_becomes_unknown_without_blind_replay():
    async def body(session):  # noqa: ANN001
        _owner, _foreign, shop, _other = await _seed(session)
        verification = await storefront_admin.claim_external_channel(
            session, shop.id, _ctx(1, "channel-expired"), channel_id="@example_channel")
        assert isinstance(verification, storefront_admin.ChannelVerification)
        command = await session.get(StorefrontApiCommand, verification.command_id)
        command.lease_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
        await session.commit()
        with pytest.raises(storefront_admin.AdminCommandError) as exc_info:
            await storefront_admin.finalize_verified_channel(
                session, verification, _ctx(1, "channel-expired"), verified=True,
                resolved_link="https://t.me/example_channel",
            )
        assert exc_info.value.code == "unknown"
        refreshed = await session.get(StorefrontApiCommand, verification.command_id)
        assert refreshed.status == "unknown" and refreshed.lease_expires_at is None
        assert shop.channel_id is None

    _run(body)


def test_audit_failure_rolls_back_db_mutation(monkeypatch):
    async def body(session):  # noqa: ANN001
        _owner, _foreign, shop, _other = await _seed(session)

        async def fail_audit(*_args, **_kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr(storefront_audit, "append_event", fail_audit)
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await storefront_admin.create_plan(
                session, shop.id, _ctx(1), gb=10, days=30, price_toman=100_000)
        assert await session.scalar(select(func.count(StorefrontPlan.id))) == 0
        assert await session.scalar(select(StorefrontBot.config_version)) == 1
        assert await session.scalar(select(func.count(StorefrontApiCommand.id))) == 0

    _run(body)


def test_replay_survives_later_child_and_partial_settings_changes():
    async def body(session):  # noqa: ANN001
        _owner, _foreign, shop, _other = await _seed(session)
        created = await storefront_admin.create_plan(
            session, shop.id, _ctx(1, "original-create"), gb=10, days=30, price_toman=100_000)
        plan_id = created.body["plan"]["id"]
        updated = await storefront_admin.update_plan(
            session, shop.id, plan_id, _ctx(2, "later-update"), gb=20)
        assert updated.body["plan"]["gb"] == 20
        replay = await storefront_admin.create_plan(
            session, shop.id, _ctx(1, "original-create"), gb=10, days=30, price_toman=100_000)
        assert replay.replayed is True and replay.body == created.body

        deleted = await storefront_admin.delete_plan(
            session, shop.id, plan_id, _ctx(3, "original-delete"))
        delete_replay = await storefront_admin.delete_plan(
            session, shop.id, plan_id, _ctx(3, "original-delete"))
        assert delete_replay.replayed is True and delete_replay.body == deleted.body

        first = await storefront_admin.update_messages(
            session, shop.id, _ctx(4, "partial-message"), welcome_text="hello")
        await storefront_admin.update_messages(
            session, shop.id, _ctx(5, "later-message"), support_contact="@support")
        partial_replay = await storefront_admin.update_messages(
            session, shop.id, _ctx(4, "partial-message"), welcome_text="hello")
        assert partial_replay.replayed is True and partial_replay.body == first.body
        assert partial_replay.body["messages"]["support_contact"] is None

    _run(body)


def test_failed_command_replay_surfaces_same_error():
    async def body(session):  # noqa: ANN001
        _owner, _foreign, shop, _other = await _seed(session)
        for attempt in range(2):
            with pytest.raises(storefront_admin.AdminCommandError) as exc_info:
                await storefront_admin.set_channel_required(
                    session, shop.id, _ctx(1, "missing-channel"), required=True)
            assert exc_info.value.code == "validation"
            assert exc_info.value.response_status == 422
            assert exc_info.value.response_body == {"error": "validation", "config_version": 1}
            if attempt == 1:
                assert await session.scalar(select(func.count(StorefrontApiCommand.id))) == 1

    _run(body)


def test_audit_role_is_derived_for_owner_and_coadmin():
    async def body(session):  # noqa: ANN001
        _owner, _foreign, shop, _other = await _seed(session)
        shop.co_admin_ids = "333"
        await session.commit()
        await storefront_admin.update_trial(
            session, shop.id, _ctx(1, "owner-role", source="bot"), enabled=False)
        await storefront_admin.update_trial(
            session, shop.id,
            _ctx(2, "coadmin-role", actor=333, source="bot"), enabled=True)
        events = (
            await session.execute(
                select(StorefrontAuditEvent)
                .where(StorefrontAuditEvent.action == "settings.trial")
                .order_by(StorefrontAuditEvent.id)
            )
        ).scalars().all()
        assert [(event.actor_telegram_id, event.actor_role) for event in events] == [
            (111, "owner"), (333, "co_admin")]

    _run(body)


@pytest.mark.pg_contract
@requires_pg
def test_pg_config_cas_and_idempotency_races():
    async def run():
        import uuid

        suffix = uuid.uuid4().hex[:10]
        engine, factory = make_engine()
        try:
            async with factory() as session:
                panel = Panel(
                    key=f"rb{suffix}", host=f"{suffix}.invalid", proxy_path_enc="x",
                    owner_uuid=f"owner-{suffix}")
                session.add(panel)
                await session.flush()
                reseller = Reseller(
                    panel_id=panel.id, admin_uuid=f"admin-{suffix}", name="race",
                    bot_chat_id=9_000_000_000 + int(suffix[:5], 16))
                session.add(reseller)
                await session.flush()
                shop = StorefrontBot(
                    reseller_id=reseller.id, panel_id=panel.id, bot_token_enc="x",
                    pay_card_enabled=False)
                session.add(shop)
                await session.commit()
                # This test races the command layer, not the owner's trial budget — pin the
                # ceiling so the `gb=7` payload below stays legal independently of the default.
                await settings_service.set_value(session, "storefront_trial_max_gb", 100)
                settings_service.clear_settings_cache()
                ids = panel.id, reseller.id, shop.id, reseller.bot_chat_id

            async def mutate(key: str):
                async with factory() as session:
                    return await storefront_admin.update_trial(
                        session, ids[2], _ctx(1, key, actor=ids[3]), enabled=False)

            first, second = await asyncio.gather(
                mutate("cas-a"), mutate("cas-b"), return_exceptions=True)
            results = [item for item in (first, second) if isinstance(
                item, storefront_admin.CommandResult)]
            conflicts = [item for item in (first, second) if isinstance(
                item, storefront_admin.AdminCommandError) and item.code == "config_conflict"]
            assert len(results) == len(conflicts) == 1, (first, second)

            # A fresh shop version: same key/payload races to one mutation and one durable command.
            async with factory() as session:
                current = await session.scalar(select(StorefrontBot.config_version).where(
                    StorefrontBot.id == ids[2]))

            async def same_key():
                async with factory() as session:
                    return await storefront_admin.update_trial(
                        session, ids[2], _ctx(current, "same-key", actor=ids[3]), gb=7)

            a, b = await asyncio.gather(same_key(), same_key(), return_exceptions=True)
            assert sum(isinstance(item, storefront_admin.CommandResult) for item in (a, b)) >= 1
            async with factory() as session:
                assert await session.scalar(select(func.count(StorefrontApiCommand.id)).where(
                    StorefrontApiCommand.storefront_bot_id == ids[2],
                    StorefrontApiCommand.idempotency_key == "same-key")) == 1
                assert await session.scalar(select(StorefrontBot.config_version).where(
                    StorefrontBot.id == ids[2])) == current + 1
                await session.execute(delete(StorefrontAuditEvent).where(
                    StorefrontAuditEvent.storefront_bot_id == ids[2]))
                await session.execute(delete(StorefrontBot).where(StorefrontBot.id == ids[2]))
                await session.execute(delete(Reseller).where(Reseller.id == ids[1]))
                await session.execute(delete(Panel).where(Panel.id == ids[0]))
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_add_manager_rejects_banned_owner_full_and_invalid():
    """The audited manager command enforces every legacy `add_co_admin` guard: a non-positive/oversized
    id, the owner, a banned customer, and the cap are all rejected; a fresh valid id still succeeds."""
    async def body(session):  # noqa: ANN001
        owner, _foreign, shop, _other = await _seed(session)

        # Invalid ids (rejected before the command is even claimed).
        for bad in (0, -5, 2**63):
            with pytest.raises(storefront_admin.AdminCommandError) as inv:
                await storefront_admin.add_manager(
                    session, shop.id, _ctx(1, f"inv-{bad}"), telegram_id=bad)
            assert inv.value.code == "validation"

        # The owner cannot be appointed as their own manager.
        with pytest.raises(storefront_admin.AdminCommandError) as is_owner:
            await storefront_admin.add_manager(
                session, shop.id, _ctx(1, "owner"), telegram_id=owner.bot_chat_id)
        assert is_owner.value.code == "validation"

        # A currently-banned customer cannot be appointed (would silently un-ban for admin use).
        session.add(StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=777, banned=True))
        await session.commit()
        with pytest.raises(storefront_admin.AdminCommandError) as banned:
            await storefront_admin.add_manager(
                session, shop.id, _ctx(1, "banned"), telegram_id=777)
        assert banned.value.code == "validation"

        # At the cap → refused (seed the id list directly to the ceiling; no version change).
        shop.co_admin_ids = ",".join(str(i) for i in range(500, 500 + storefront.MAX_CO_ADMINS))
        await session.commit()
        with pytest.raises(storefront_admin.AdminCommandError) as full:
            await storefront_admin.add_manager(
                session, shop.id, _ctx(1, "full"), telegram_id=999)
        assert full.value.code == "validation"

        # A fresh valid id still succeeds once there is room.
        shop.co_admin_ids = None
        await session.commit()
        ok = await storefront_admin.add_manager(
            session, shop.id, _ctx(1, "ok"), telegram_id=888)
        assert ok.body["managers"]["co_admin_ids"] == [888]

    _run(body)


def test_customer_ban_allows_bot_coadmin_refuses_foreign_and_is_visible_to_portal():
    """Parity: the bot ban and the portal ban are the SAME audited command over the same field.
    A bot co-admin may ban (customer ban is not owner-only); a non-admin id is refused; the
    resulting banned flag is what portal reads return."""
    async def body(session):  # noqa: ANN001
        owner, _foreign, shop, _other = await _seed(session)
        cust = StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=901, name="X")
        session.add(cust)
        await session.flush()
        shop.co_admin_ids = "333"
        await session.commit()

        coadmin = storefront_admin.CommandContext(
            actor_telegram_id=333, actor_role="admin", source="bot",
            idempotency_key="ban-bot-1", expected_version=1)
        res = await storefront_admin.set_customer_banned(
            session, shop.id, cust.id, coadmin, banned=True, reason="via bot test")
        assert res.response_status == 200
        assert (await session.get(StorefrontCustomer, cust.id)).banned is True

        stranger = storefront_admin.CommandContext(
            actor_telegram_id=999, actor_role="admin", source="bot",
            idempotency_key="ban-bot-2", expected_version=1)
        with pytest.raises(storefront_admin.AdminCommandError) as exc:
            await storefront_admin.set_customer_banned(
                session, shop.id, cust.id, stranger, banned=False, reason="not allowed here")
        assert exc.value.code == "not_found"
        # the stranger's attempt changed nothing
        assert (await session.get(StorefrontCustomer, cust.id)).banned is True

    _run(body)


@pytest.mark.pg_contract
@requires_pg
def test_pg_customer_ban_idempotency_race():
    """Two concurrent bans with the SAME idempotency key must collapse to ONE durable command
    row (uq_sfcommand_actor_key) and one applied ban, on real Postgres 16 under contention."""
    async def run():
        import uuid
        suffix = uuid.uuid4().hex[:10]
        engine, factory = make_engine()
        try:
            async with factory() as session:
                panel = Panel(key=f"cb{suffix}", host=f"{suffix}.invalid", proxy_path_enc="x",
                              owner_uuid=f"o-{suffix}")
                session.add(panel)
                await session.flush()
                reseller = Reseller(panel_id=panel.id, admin_uuid=f"a-{suffix}", name="race",
                                    bot_chat_id=9_100_000_000 + int(suffix[:5], 16))
                session.add(reseller)
                await session.flush()
                shop = StorefrontBot(reseller_id=reseller.id, panel_id=panel.id, bot_token_enc="x",
                                     pay_card_enabled=False)
                session.add(shop)
                await session.flush()
                cust = StorefrontCustomer(storefront_bot_id=shop.id,
                                          telegram_id=7_000_000 + int(suffix[:4], 16), name="C")
                session.add(cust)
                await session.commit()
                ids = panel.id, reseller.id, shop.id, cust.id, reseller.bot_chat_id

            async def ban():
                async with factory() as session:
                    return await storefront_admin.set_customer_banned(
                        session, ids[2], ids[3],
                        storefront_admin.CommandContext(
                            actor_telegram_id=ids[4], actor_role="owner", source="portal",
                            idempotency_key="ban-race", expected_version=1),
                        banned=True, reason="concurrent ban race")

            a, b = await asyncio.gather(ban(), ban(), return_exceptions=True)
            assert sum(isinstance(x, storefront_admin.CommandResult) for x in (a, b)) >= 1, (a, b)
            async with factory() as session:
                assert await session.scalar(select(func.count(StorefrontApiCommand.id)).where(
                    StorefrontApiCommand.storefront_bot_id == ids[2],
                    StorefrontApiCommand.idempotency_key == "ban-race")) == 1
                assert (await session.get(StorefrontCustomer, ids[3])).banned is True
                await session.execute(delete(StorefrontAuditEvent).where(
                    StorefrontAuditEvent.storefront_bot_id == ids[2]))
                await session.execute(delete(StorefrontApiCommand).where(
                    StorefrontApiCommand.storefront_bot_id == ids[2]))
                await session.execute(delete(StorefrontCustomer).where(
                    StorefrontCustomer.id == ids[3]))
                await session.execute(delete(StorefrontBot).where(StorefrontBot.id == ids[2]))
                await session.execute(delete(Reseller).where(Reseller.id == ids[1]))
                await session.execute(delete(Panel).where(Panel.id == ids[0]))
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_money_commands_work_from_the_bot_source_and_are_idempotent():
    """Parity: a bot co-admin (source='bot') can confirm a top-up and adjust a wallet through the
    SAME shared money commands the portal uses; both are idempotent (no double credit)."""
    from decimal import Decimal
    async def body(session):  # noqa: ANN001
        owner, _foreign, shop, _other = await _seed(session)
        shop.co_admin_ids = "333"
        cust = StorefrontCustomer(storefront_bot_id=shop.id, telegram_id=901, name="X",
                                  wallet_balance_toman=0)
        session.add(cust)
        await session.flush()
        topup = StorefrontWalletTxn(
            storefront_bot_id=shop.id, customer_id=cust.id, kind="topup",
            amount_toman=Decimal("50000"), requested_amount_toman=Decimal("50000"),
            status="pending", method="card")
        session.add(topup)
        await session.commit()

        def ctx(key):  # noqa: ANN001, ANN202 — co-admin via the bot
            return storefront_admin.CommandContext(
                actor_telegram_id=333, actor_role="admin", source="bot",
                idempotency_key=key, expected_version=1)

        # Bot co-admin confirms → credits once; replay of the same key does not double-credit.
        r = await storefront_admin.set_topup_decision(
            session, shop.id, topup.id, ctx("bot-confirm"), decision="confirm")
        assert r.body["changed"] is True and r.body["credited"] == 50_000
        assert int((await session.get(StorefrontCustomer, cust.id)).wallet_balance_toman) == 50_000
        await storefront_admin.set_topup_decision(
            session, shop.id, topup.id, ctx("bot-confirm"), decision="confirm")  # replay
        assert int((await session.get(StorefrontCustomer, cust.id)).wallet_balance_toman) == 50_000

        # Bot co-admin adjusts (+10,000) → 60,000; replay same key → still 60,000.
        await storefront_admin.adjust_wallet(
            session, shop.id, cust.id, ctx("bot-adj"), amount_toman_signed=10_000, reason="via bot")
        assert int((await session.get(StorefrontCustomer, cust.id)).wallet_balance_toman) == 60_000
        await storefront_admin.adjust_wallet(
            session, shop.id, cust.id, ctx("bot-adj"), amount_toman_signed=10_000, reason="via bot")
        assert int((await session.get(StorefrontCustomer, cust.id)).wallet_balance_toman) == 60_000

    _run(body)
