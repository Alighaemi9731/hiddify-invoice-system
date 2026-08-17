"""Shared absolute-state administration commands for storefront bot and portal.

All DB-only commands use one transaction for: idempotency claim, atomic config-version CAS, domain
mutation, redacted audit and cached response.  Functions authorize the actor and child ownership in
the service layer; callers cannot make a foreign id safe by routing alone.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.models import (
    Reseller,
    StorefrontApiCommand,
    StorefrontAuditEvent,
    StorefrontBot,
    StorefrontBroadcastJob,
    StorefrontCreditCode,
    StorefrontCustomer,
    StorefrontOrder,
    StorefrontPlan,
    StorefrontWalletTxn,
)
from app.services import (
    periods,
    settings_service,
    storefront,
    storefront_audit,
    storefront_credit,
    storefront_pricing,
)

log = logging.getLogger("bot.storefront")

Source = Literal["bot", "portal", "system"]
_BSC_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}\Z")
_TON_ADDRESS = re.compile(r"(?:EQ|UQ|kQ|0Q)[A-Za-z0-9_-]{46}\Z")
_TON_RAW_ADDRESS = re.compile(r"(?:-1|0):[0-9a-fA-F]{64}\Z")
_CHANNEL_ID = re.compile(r"(?:-?[1-9][0-9]{0,19}|@[A-Za-z0-9_]{5,32})\Z")
_MAX_I64 = 2**63 - 1
_UNSET = object()


def _as_utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(
        dt.timezone.utc
    )


def _iso(value: dt.datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _jsonable(mapping: dict) -> dict:
    """A JSON-safe copy of a field dict for the idempotency-claim intent (datetimes → ISO strings)."""
    return {k: (_iso(v) if isinstance(v, dt.datetime) else v) for k, v in mapping.items()}


class AdminCommandError(Exception):
    def __init__(
        self, code: str, message: str, *, current_version: int | None = None,
        response_status: int | None = None, response_body: dict | list | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.current_version = current_version
        self.response_status = response_status
        self.response_body = response_body


@dataclass(frozen=True)
class CommandContext:
    actor_telegram_id: int
    actor_role: str
    source: Source
    idempotency_key: str
    expected_version: int
    correlation_id: str | None = None


@dataclass(frozen=True)
class CommandResult:
    response_status: int
    body: dict | list | None
    config_version: int
    replayed: bool = False


@dataclass(frozen=True)
class ChannelVerification:
    command_id: int
    action: str
    shop_id: int
    expected_version: int
    credential_fingerprint: str
    channel_id: str


@dataclass
class _Mutation:
    body: dict | list | None
    after: dict | list | None
    entity_type: str | None = None
    entity_id: str | int | None = None
    response_status: int = 200


async def _authorized_shop(
    session: AsyncSession,
    shop_id: int,
    actor_id: int,
    source: Source,
    *,
    owner_only: bool = False,
) -> tuple[StorefrontBot, Reseller]:
    row = (
        await session.execute(
            select(StorefrontBot, Reseller)
            .join(Reseller, Reseller.id == StorefrontBot.reseller_id)
            .where(StorefrontBot.id == shop_id)
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).first()
    if row is None:
        raise AdminCommandError("not_found", "Storefront not found")
    shop, reseller = row
    is_owner = reseller.bot_chat_id == actor_id
    allowed = source == "system" or is_owner or (
        source == "bot" and actor_id in storefront.co_admin_ids(shop)
    )
    if not allowed or (owner_only and not (is_owner or source == "system")):
        # Foreign and unauthorized ids share one response to avoid shop enumeration.
        raise AdminCommandError("not_found", "Storefront not found")
    return shop, reseller


async def _owned_plan(
    session: AsyncSession, shop_id: int, plan_id: int
) -> StorefrontPlan:
    plan = await session.get(StorefrontPlan, plan_id)
    if plan is None or plan.storefront_bot_id != shop_id:
        raise AdminCommandError("not_found", "Plan not found")
    return plan


async def _audit_role(
    session: AsyncSession, shop: StorefrontBot, ctx: CommandContext
) -> str:
    """Derive the durable role from DB authorization state; never trust caller-supplied role text."""
    if ctx.source == "system":
        return "system"
    reseller = await session.get(
        Reseller, shop.reseller_id, populate_existing=True)
    if reseller is not None and reseller.bot_chat_id == ctx.actor_telegram_id:
        return "owner"
    return "co_admin"


async def _cas(session: AsyncSession, shop: StorefrontBot, expected: int) -> int | None:
    result = await session.execute(
        update(StorefrontBot)
        .where(StorefrontBot.id == shop.id, StorefrontBot.config_version == expected)
        .values(config_version=StorefrontBot.config_version + 1)
        .execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:  # type: ignore[attr-defined]
        return None
    shop.config_version = expected + 1
    return expected + 1


async def _current_version(session: AsyncSession, shop_id: int) -> int:
    value = await session.scalar(
        select(StorefrontBot.config_version).where(StorefrontBot.id == shop_id)
    )
    return int(value or 1)


def _replay_result(command: StorefrontApiCommand, ctx: CommandContext) -> CommandResult:
    body = command.response_body
    version = int(body.get("config_version", ctx.expected_version)) if isinstance(body, dict) else (
        ctx.expected_version)
    status = int(command.response_status or 200)
    if command.status == "failed" or status >= 400:
        code = command.error_class or "replayed_failure"
        raise AdminCommandError(
            code, "Previous command attempt failed", current_version=version,
            response_status=status, response_body=body,
        )
    return CommandResult(status, body, version, replayed=True)


async def _claim_db_command(
    session: AsyncSession, shop: StorefrontBot, ctx: CommandContext, *, action: str, intent: dict,
) -> tuple[StorefrontApiCommand, CommandResult | None]:
    """Claim using normalized client intent before reading any mutable child/current state."""
    if ctx.expected_version < 1:
        raise AdminCommandError("validation", "expected_version must be positive")
    claim = await storefront_audit.claim_command(
        session,
        shop_id=shop.id,
        actor_telegram_id=ctx.actor_telegram_id,
        idempotency_key=ctx.idempotency_key,
        action=action,
        request={"expected_version": ctx.expected_version, **intent},
    )
    if claim.outcome == "conflict":
        raise AdminCommandError("idempotency_conflict", "Idempotency key payload conflict")
    if claim.outcome == "in_flight":
        raise AdminCommandError("in_flight", "Command is already in progress")
    if claim.outcome == "unknown":
        shop.channel_verification_error = "unknown"
        await session.commit()
        raise AdminCommandError("unknown", "Command outcome requires reconciliation")
    if claim.outcome == "replay":
        return claim.command, _replay_result(claim.command, ctx)
    return claim.command, None


async def _cache_known_failure(
    session: AsyncSession, shop: StorefrontBot, ctx: CommandContext, *, action: str,
    command: StorefrontApiCommand, exc: AdminCommandError,
) -> None:
    status = exc.response_status or {
        "not_found": 404, "validation": 422, "below_cost": 422, "config_conflict": 409,
    }.get(exc.code, 409)
    current = await _current_version(session, shop.id)
    body = exc.response_body or {"error": exc.code, "config_version": current}
    await storefront_audit.finalize_command(
        session, command, succeeded=False, response_status=status, response_body=body,
        error_class=exc.code,
    )
    await storefront_audit.append_event(
        session,
        storefront_bot_id=shop.id, actor_telegram_id=ctx.actor_telegram_id,
        actor_role=await _audit_role(session, shop, ctx), source=ctx.source,
        action=action, outcome="failed",
        correlation_id=ctx.correlation_id or ctx.idempotency_key, error_class=exc.code,
    )
    await session.commit()
    exc.current_version = current
    exc.response_status = status
    exc.response_body = body


async def _execute(
    session: AsyncSession,
    shop: StorefrontBot,
    ctx: CommandContext,
    *,
    action: str,
    command: StorefrontApiCommand,
    before: dict | list | None,
    mutate: Callable[[int], Awaitable[_Mutation]],
) -> CommandResult:
    new_version = await _cas(session, shop, ctx.expected_version)
    if new_version is None:
        current = await _current_version(session, shop.id)
        body = {"error": "config_conflict", "config_version": current}
        await storefront_audit.finalize_command(
            session, command, succeeded=False, response_status=409, response_body=body,
            error_class="config_conflict",
        )
        await storefront_audit.append_event(
            session,
            storefront_bot_id=shop.id,
            actor_telegram_id=ctx.actor_telegram_id,
            actor_role=await _audit_role(session, shop, ctx),
            source=ctx.source,
            action=action,
            outcome="conflict",
            correlation_id=ctx.correlation_id or ctx.idempotency_key,
            before=before,
            error_class="config_conflict",
        )
        await session.commit()
        raise AdminCommandError(
            "config_conflict", "Configuration changed", current_version=current,
            response_status=409, response_body=body,
        )

    try:
        mutation = await mutate(new_version)
        response_body: dict | list | None = mutation.body
        if isinstance(response_body, dict):
            response_body = {**response_body, "config_version": new_version}
        response_body = storefront_audit.safe_cached_response(response_body)
        await storefront_audit.append_event(
            session,
            storefront_bot_id=shop.id,
            actor_telegram_id=ctx.actor_telegram_id,
            actor_role=await _audit_role(session, shop, ctx),
            source=ctx.source,
            action=action,
            outcome="succeeded",
            entity_type=mutation.entity_type,
            entity_id=mutation.entity_id,
            correlation_id=ctx.correlation_id or ctx.idempotency_key,
            before=before,
            after=mutation.after,
        )
        await storefront_audit.finalize_command(
            session, command, succeeded=True, response_status=mutation.response_status,
            response_body=response_body,
        )
        await session.commit()
        return CommandResult(mutation.response_status, response_body, new_version)
    except Exception:
        await session.rollback()
        raise


def _plan_dict(plan: StorefrontPlan) -> dict:
    return {
        "id": plan.id, "gb": plan.gb, "days": plan.days,
        "price_toman": plan.price_toman, "enabled": plan.enabled, "sort_order": plan.sort_order,
    }


def _assert_price_covers_cost(
    *, cost: int, gb: int, price_toman: int, action: storefront_pricing.Action = "save",
) -> None:
    """Refuse a plan priced below what that quota costs the reseller (see `storefront_pricing`).

    Raised as `below_cost` rather than `validation` so each surface can react differently: the bot
    keeps the reseller in the price prompt to retype, the portal renders a field-level error.
    """
    if not storefront_pricing.is_below_cost(cost=cost, gb=gb, price_toman=price_toman):
        return
    raise AdminCommandError(
        "below_cost",
        storefront_pricing.below_cost_message_fa(
            cost=cost, gb=gb, price_toman=price_toman, action=action),
        response_status=422,
        response_body=storefront_pricing.below_cost_body(
            cost=cost, gb=gb, price_toman=price_toman),
    )


def _validate_plan(*, gb: int, days: int, price_toman: int) -> tuple[int, int, int]:
    """A plan is exactly quota + duration + price. It has no title: the field existed only in the
    portal, was invisible in the bot on both the admin and the customer side, and left every
    bot-made plan permanently unnamed (owner decision, 2026-08-18). The `storefront_plans.title`
    column survives unused so old audit rows stay readable."""
    if not 1 <= int(gb) <= 100_000:
        raise AdminCommandError("validation", "gb must be between 1 and 100000")
    if not 1 <= int(days) <= 3650:
        raise AdminCommandError("validation", "days must be between 1 and 3650")
    if not 0 <= int(price_toman) <= 10**12:
        raise AdminCommandError("validation", "price_toman is out of range")
    return int(gb), int(days), int(price_toman)


async def list_plans(
    session: AsyncSession, shop_id: int, actor_id: int, source: Source
) -> dict:
    shop, _ = await _authorized_shop(session, shop_id, actor_id, source)
    plans = await storefront.list_plans(session, shop.id)
    return {"config_version": shop.config_version, "plans": [_plan_dict(plan) for plan in plans]}


async def create_plan(
    session: AsyncSession, shop_id: int, ctx: CommandContext, *,
    gb: int, days: int, price_toman: int,
) -> CommandResult:
    gb, days, price_toman = _validate_plan(gb=gb, days=days, price_toman=price_toman)
    shop, reseller = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    request = {"gb": gb, "days": days, "price_toman": price_toman}
    command, replay = await _claim_db_command(
        session, shop, ctx, action="plan.create", intent=request)
    if replay is not None:
        return replay
    try:
        _assert_price_covers_cost(
            cost=await storefront_pricing.cost_per_gb(session, reseller),
            gb=gb, price_toman=price_toman)
        plan_count = len(await storefront.list_plans(session, shop.id))
        if plan_count >= 500:
            raise AdminCommandError("validation", "a storefront can have at most 500 plans")
    except AdminCommandError as exc:
        await _cache_known_failure(
            session, shop, ctx, action="plan.create", command=command, exc=exc)
        raise

    async def mutate(_version: int) -> _Mutation:
        plan = StorefrontPlan(
            storefront_bot_id=shop.id, gb=gb, days=days,
            price_toman=price_toman, enabled=True, sort_order=plan_count,
        )
        session.add(plan)
        await session.flush()
        after = _plan_dict(plan)
        return _Mutation({"plan": after}, after, "plan", plan.id, 201)

    return await _execute(
        session, shop, ctx, action="plan.create", command=command, before=None, mutate=mutate)


async def update_plan(
    session: AsyncSession, shop_id: int, plan_id: int, ctx: CommandContext, *,
    gb: int | None = None, days: int | None = None, price_toman: int | None = None,
) -> CommandResult:
    """Patch one or more of a plan's three fields. Every caller (portal form, bot field picker)
    sends only what changed; the below-cost guard below judges the MERGED values."""
    intent: dict[str, Any] = {"plan_id": plan_id}
    for name, value, lo, hi in (
        ("gb", gb, 1, 100_000), ("days", days, 1, 3650),
        ("price_toman", price_toman, 0, 10**12),
    ):
        if value is not None:
            normalized = int(value)
            if not lo <= normalized <= hi:
                raise AdminCommandError("validation", f"{name} is out of range")
            intent[name] = normalized
    if len(intent) == 1:
        raise AdminCommandError("validation", "empty plan update")
    shop, reseller = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    command, replay = await _claim_db_command(
        session, shop, ctx, action="plan.update", intent=intent)
    if replay is not None:
        return replay
    try:
        plan = await _owned_plan(session, shop.id, plan_id)
        before = _plan_dict(plan)
        values = _validate_plan(
            gb=int(intent.get("gb", plan.gb)),
            days=int(intent.get("days", plan.days)),
            price_toman=int(intent.get("price_toman", plan.price_toman)),
        )
        # Checked on the MERGED values: a PATCH of price alone must be judged against the stored
        # gb (and a PATCH of gb alone against the stored price).
        _assert_price_covers_cost(
            cost=await storefront_pricing.cost_per_gb(session, reseller),
            gb=values[0], price_toman=values[2])
    except AdminCommandError as exc:
        await _cache_known_failure(
            session, shop, ctx, action="plan.update", command=command, exc=exc)
        raise

    async def mutate(_version: int) -> _Mutation:
        plan.gb, plan.days, plan.price_toman = values
        await session.flush()
        after = _plan_dict(plan)
        return _Mutation({"plan": after}, after, "plan", plan.id)

    return await _execute(
        session, shop, ctx, action="plan.update", command=command,
        before=before, mutate=mutate,
    )


async def set_plan_enabled(
    session: AsyncSession, shop_id: int, plan_id: int, ctx: CommandContext, *, enabled: bool,
) -> CommandResult:
    shop, reseller = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    intent = {"plan_id": plan_id, "enabled": bool(enabled)}
    command, replay = await _claim_db_command(
        session, shop, ctx, action="plan.set_enabled", intent=intent)
    if replay is not None:
        return replay
    try:
        plan = await _owned_plan(session, shop.id, plan_id)
        # Enable-only: DISABLING a below-cost plan must always stay possible — it is the remedy,
        # and the below-cost sweep depends on it.
        if enabled:
            _assert_price_covers_cost(
                cost=await storefront_pricing.cost_per_gb(session, reseller),
                gb=plan.gb, price_toman=plan.price_toman, action="enable")
    except AdminCommandError as exc:
        await _cache_known_failure(
            session, shop, ctx, action="plan.set_enabled", command=command, exc=exc)
        raise
    before = _plan_dict(plan)

    async def mutate(_version: int) -> _Mutation:
        plan.enabled = bool(enabled)
        await session.flush()
        after = _plan_dict(plan)
        return _Mutation({"plan": after}, after, "plan", plan.id)

    return await _execute(
        session, shop, ctx, action="plan.set_enabled",
        command=command, before=before, mutate=mutate,
    )


async def delete_plan(
    session: AsyncSession, shop_id: int, plan_id: int, ctx: CommandContext,
) -> CommandResult:
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    command, replay = await _claim_db_command(
        session, shop, ctx, action="plan.delete", intent={"plan_id": plan_id})
    if replay is not None:
        return replay
    try:
        plan = await _owned_plan(session, shop.id, plan_id)
    except AdminCommandError as exc:
        await _cache_known_failure(
            session, shop, ctx, action="plan.delete", command=command, exc=exc)
        raise
    before = _plan_dict(plan)

    async def mutate(_version: int) -> _Mutation:
        await session.delete(plan)
        await session.flush()
        return _Mutation({"deleted_id": plan_id}, None, "plan", plan_id)

    return await _execute(
        session, shop, ctx, action="plan.delete", command=command,
        before=before, mutate=mutate,
    )


async def reorder_plans(
    session: AsyncSession, shop_id: int, ctx: CommandContext, *, ordered_ids: list[int],
) -> CommandResult:
    if len(set(ordered_ids)) != len(ordered_ids):
        raise AdminCommandError("validation", "ordered_ids contains duplicates")
    normalized_ids = [int(item) for item in ordered_ids]
    if len(normalized_ids) > 500:
        raise AdminCommandError("validation", "a storefront can have at most 500 plans")
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    command, replay = await _claim_db_command(
        session, shop, ctx, action="plan.reorder", intent={"ordered_ids": normalized_ids})
    if replay is not None:
        return replay
    plans = await storefront.list_plans(session, shop.id)
    existing_ids = [plan.id for plan in plans]
    if set(normalized_ids) != set(existing_ids):
        exc = AdminCommandError("validation", "ordered_ids must contain every plan exactly once")
        await _cache_known_failure(
            session, shop, ctx, action="plan.reorder", command=command, exc=exc)
        raise exc
    before = [_plan_dict(plan) for plan in plans]
    by_id = {plan.id: plan for plan in plans}

    async def mutate(_version: int) -> _Mutation:
        for index, plan_id in enumerate(normalized_ids):
            by_id[plan_id].sort_order = index
        await session.flush()
        after = [_plan_dict(by_id[plan_id]) for plan_id in normalized_ids]
        return _Mutation(
            {"ordered_ids": normalized_ids, "count": len(normalized_ids)},
            after, "plan_order", shop.id)

    return await _execute(
        session, shop, ctx, action="plan.reorder", command=command,
        before=before, mutate=mutate,
    )


async def plan_history(
    session: AsyncSession, shop_id: int, plan_id: int, actor_id: int, source: Source,
) -> list[dict]:
    await _authorized_shop(session, shop_id, actor_id, source)
    rows = list((
        await session.execute(
            select(StorefrontAuditEvent)
            .where(
                StorefrontAuditEvent.storefront_bot_id == shop_id,
                StorefrontAuditEvent.entity_type.in_(("plan", "plan_order")),
            )
            .order_by(StorefrontAuditEvent.created_at.desc(), StorefrontAuditEvent.id.desc())
        )
    ).scalars().all())
    rows = [
        row for row in rows
        if (row.entity_type == "plan" and row.entity_id == str(plan_id))
        or (
            row.entity_type == "plan_order"
            and any(
                isinstance(item, dict) and item.get("id") == plan_id
                for item in [*(row.before_json or []), *(row.after_json or [])]
            )
        )
    ]
    if not rows:
        current = await session.get(StorefrontPlan, plan_id)
        if current is None or current.storefront_bot_id != shop_id:
            raise AdminCommandError("not_found", "Plan not found")
    return [
        {
            "id": row.id, "action": row.action, "before": row.before_json,
            "after": row.after_json, "actor_role": row.actor_role,
            "actor_telegram_id": row.actor_telegram_id, "source": row.source,
            "outcome": row.outcome, "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


def settings_snapshot(shop: StorefrontBot) -> dict:
    return {
        "config_version": shop.config_version,
        "payment": {
            "card_enabled": shop.pay_card_enabled, "card_number": shop.card_number,
            "card_holder": shop.card_holder, "usdt_enabled": shop.pay_usdt_enabled,
            "usdt_address": shop.usdt_address, "ton_enabled": shop.pay_ton_enabled,
            "ton_address": shop.ton_address,
        },
        "trial": {
            "enabled": shop.free_trial_enabled, "gb": shop.free_trial_gb,
            "days": shop.free_trial_days,
        },
        "messages": {"welcome_text": shop.welcome_text, "support_contact": shop.support_contact},
        "shop_state": {"closed": shop.shop_closed, "closed_text": shop.closed_text},
        "channel": {
            "required": shop.channel_required, "channel_id": shop.channel_id,
            "channel_link": shop.channel_link,
            "verified_at": shop.channel_verified_at,
            "verification_error": shop.channel_verification_error,
        },
    }


async def get_settings(
    session: AsyncSession, shop_id: int, actor_id: int, source: Source,
) -> dict:
    shop, _ = await _authorized_shop(session, shop_id, actor_id, source)
    return settings_snapshot(shop)


def _digits(value: str) -> str:
    out = []
    for char in value.strip():
        if char.isdigit():
            out.append(str(unicodedata.digit(char)))
        elif char not in " -":
            raise AdminCommandError("validation", "card_number contains invalid characters")
    return "".join(out)


async def update_payment(
    session: AsyncSession, shop_id: int, ctx: CommandContext, **changes: Any,
) -> CommandResult:
    allowed = {
        "card_enabled", "card_number", "card_holder", "usdt_enabled", "usdt_address",
        "ton_enabled", "ton_address",
    }
    if not changes or set(changes) - allowed:
        raise AdminCommandError("validation", "unknown or empty payment settings")
    intent: dict[str, Any] = {}
    for key, value in changes.items():
        if key == "card_number":
            value = _digits(str(value or "")) or None
            if value is not None and len(value) != 16:
                raise AdminCommandError("validation", "card_number must contain 16 digits")
        elif key == "card_holder":
            value = str(value or "").strip() or None
            if value is not None and not 2 <= len(value) <= 128:
                raise AdminCommandError("validation", "card_holder is out of range")
        elif key == "usdt_address":
            value = str(value or "").strip() or None
            if value is not None and not _BSC_ADDRESS.fullmatch(value):
                raise AdminCommandError("validation", "invalid BEP-20 USDT address")
        elif key == "ton_address":
            value = str(value or "").strip() or None
            if value is not None and not (
                _TON_ADDRESS.fullmatch(value) or _TON_RAW_ADDRESS.fullmatch(value)
            ):
                raise AdminCommandError("validation", "invalid TON address")
        elif key.endswith("_enabled"):
            value = bool(value)
        intent[key] = value
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    command, replay = await _claim_db_command(
        session, shop, ctx, action="settings.payment", intent=intent)
    if replay is not None:
        return replay
    before = settings_snapshot(shop)["payment"]
    state = {**before, **intent}
    try:
        if state["card_enabled"] and (
            not state["card_number"] or len(state["card_number"]) != 16
            or not state["card_holder"] or not 2 <= len(state["card_holder"]) <= 128
        ):
            raise AdminCommandError(
                "validation", "enabled card payment requires a 16-digit card and holder")
        if state["usdt_enabled"] and not _BSC_ADDRESS.fullmatch(
            str(state["usdt_address"] or "")):
            raise AdminCommandError("validation", "invalid BEP-20 USDT address")
        if state["ton_enabled"] and not (
            _TON_ADDRESS.fullmatch(str(state["ton_address"] or ""))
            or _TON_RAW_ADDRESS.fullmatch(str(state["ton_address"] or ""))
        ):
            raise AdminCommandError("validation", "invalid TON address")
    except AdminCommandError as exc:
        await _cache_known_failure(
            session, shop, ctx, action="settings.payment", command=command, exc=exc)
        raise

    async def mutate(_version: int) -> _Mutation:
        shop.pay_card_enabled = bool(state["card_enabled"])
        shop.card_number, shop.card_holder = state["card_number"], state["card_holder"]
        shop.pay_usdt_enabled, shop.usdt_address = bool(state["usdt_enabled"]), state["usdt_address"]
        shop.pay_ton_enabled, shop.ton_address = bool(state["ton_enabled"]), state["ton_address"]
        await session.flush()
        return _Mutation(
            {"payment_updated": True, "updated_fields": sorted(intent)},
            state, "settings", "payment")

    return await _execute(
        session, shop, ctx, action="settings.payment", command=command,
        before=before, mutate=mutate,
    )


async def update_trial(
    session: AsyncSession, shop_id: int, ctx: CommandContext, *,
    enabled: bool | object = _UNSET, gb: int | object = _UNSET, days: int | object = _UNSET,
) -> CommandResult:
    if enabled is _UNSET and gb is _UNSET and days is _UNSET:
        raise AdminCommandError("validation", "empty trial settings")
    intent: dict[str, Any] = {}
    if enabled is not _UNSET:
        intent["enabled"] = bool(enabled)
    if gb is not _UNSET:
        intent["gb"] = int(gb)  # type: ignore[arg-type]
        # Bounded by the OWNER's cap, not a constant: a trial's quota is excluded from the
        # reseller's invoice entirely, so the platform owner pays for whatever a shop puts here.
        max_gb = await storefront.trial_max_gb(session)
        if not 1 <= intent["gb"] <= max_gb:
            raise AdminCommandError(
                "validation", f"حجم تست رایگان باید بین ۱ و {max_gb} گیگابایت باشد")
    if days is not _UNSET:
        intent["days"] = int(days)  # type: ignore[arg-type]
        if not 1 <= intent["days"] <= 90:
            raise AdminCommandError("validation", "trial days out of range")
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    command, replay = await _claim_db_command(
        session, shop, ctx, action="settings.trial", intent=intent)
    if replay is not None:
        return replay
    before = settings_snapshot(shop)["trial"]
    trial_enabled = bool(intent.get("enabled", shop.free_trial_enabled))
    trial_gb = int(intent.get("gb", shop.free_trial_gb))
    trial_days = int(intent.get("days", shop.free_trial_days))
    state = {"enabled": trial_enabled, "gb": trial_gb, "days": trial_days}

    async def mutate(_version: int) -> _Mutation:
        shop.free_trial_enabled, shop.free_trial_gb, shop.free_trial_days = (
            trial_enabled, trial_gb, trial_days)
        await session.flush()
        return _Mutation({"trial": state}, state, "settings", "trial")

    return await _execute(
        session, shop, ctx, action="settings.trial", command=command,
        before=before, mutate=mutate,
    )


async def trial_reset_status(session: AsyncSession, shop: StorefrontBot) -> dict:
    """What the trial-reset button should render: whether it is available, why not, and how many
    customers a reset would actually re-arm. Read-only — safe to call from a GET."""
    period = periods.current_month().label
    eligible = int((await session.execute(
        select(func.count()).select_from(StorefrontCustomer).where(
            StorefrontCustomer.storefront_bot_id == shop.id,
            StorefrontCustomer.free_trial_used.is_(True),
        )
    )).scalar_one() or 0)
    enabled = await storefront.trial_reset_enabled(session)
    done_this_month = shop.trial_reset_period == period
    return {
        "period": period,
        "last_reset_period": shop.trial_reset_period,
        "available": enabled and not done_this_month,
        "reason": (None if enabled and not done_this_month
                   else ("disabled" if not enabled else "already_reset_this_month")),
        "eligible_count": eligible,
        "max_gb": await storefront.trial_max_gb(session),
    }


async def reset_free_trials(
    session: AsyncSession, shop_id: int, ctx: CommandContext,
) -> CommandResult:
    """Re-arm every customer's free trial for one shop, AT MOST ONCE PER GREGORIAN MONTH, and
    announce it to the shop's customers.

    Why once a month, enforced by a stored `YYYY-MM` rather than a rolling timestamp: the trial's
    quota is excluded from the reseller's invoice entirely, so every reset is quota the platform
    owner gives away. Pinning it to the billing month makes the cost per shop countable in the
    same units the invoices use, and makes "have I already done it?" answerable without clock math.

    The announcement is enqueued INSIDE this transaction as a durable delivery job (the same
    machinery a normal broadcast uses, drained by the `storefront_delivery` worker) — so it can
    never fire for a rolled-back reset, and a committed reset can never lose its announcement.
    A replayed idempotency key returns the cached response without re-running any of this, so no
    customer is re-armed twice and nobody is messaged twice.
    """
    from app.services import storefront_delivery

    if not await storefront.trial_reset_enabled(session):
        raise AdminCommandError(
            "trial_reset_disabled", "ریست تست رایگان توسط مدیر سامانه غیرفعال شده است",
            response_status=422)
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    period = periods.current_month().label
    # The idempotency claim comes FIRST, before the once-a-month check. The other way round, a
    # retried request (dropped response, tapped-twice button) would be answered
    # "already reset this month" by the reset it had itself just performed, instead of replaying
    # its own cached success. Same reason `enqueue_broadcast` claims before counting its audience.
    command, replay = await _claim_db_command(
        session, shop, ctx, action="trial.reset", intent={"period": period})
    if replay is not None:
        return replay
    if shop.trial_reset_period == period:
        exc = AdminCommandError(
            "already_reset_this_month",
            f"تست‌های این فروشگاه در دورهٔ {period} یک بار ریست شده‌اند؛ "
            "ریست بعدی از ماه میلادی آینده ممکن است.",
            response_status=409,
            response_body={"error": "already_reset_this_month", "period": period})
        await _cache_known_failure(
            session, shop, ctx, action="trial.reset", command=command, exc=exc)
        raise exc

    customers = await storefront.customers_in_segment(session, shop.id, "all")
    if len(customers) > storefront.AUDIENCE_CAP:
        exc = AdminCommandError(
            "audience_too_large", f"audience exceeds {storefront.AUDIENCE_CAP}",
            response_status=422,
            response_body={"error": "audience_too_large", "count": len(customers)})
        await _cache_known_failure(
            session, shop, ctx, action="trial.reset", command=command, exc=exc)
        raise exc
    text = await _trial_reset_announcement(session, shop)
    before = {"trial_reset_period": shop.trial_reset_period}

    async def mutate(_version: int) -> _Mutation:
        # synchronize_session=False: a bulk UPDATE must not try to reconcile the ORM identity map
        # (the customer rows are not loaded here, and matching them one by one would turn one
        # statement into thousands).
        result = await session.execute(
            update(StorefrontCustomer)
            .where(
                StorefrontCustomer.storefront_bot_id == shop.id,
                StorefrontCustomer.free_trial_used.is_(True),
            )
            .values(free_trial_used=False)
            .execution_options(synchronize_session=False)
        )
        reset_count = int(getattr(result, "rowcount", 0) or 0)
        # …but a bulk UPDATE the ORM didn't synchronize leaves every already-loaded customer
        # reporting the OLD flag. `customers` above are exactly the rows just changed, so expire
        # that one attribute: anything reading it later in this session (or a caller reusing the
        # session) sees the truth, and nothing else is re-fetched.
        for customer in customers:
            session.expire(customer, ["free_trial_used"])
        shop.trial_reset_period = period
        job = await storefront_delivery.snapshot_job(
            session, storefront_bot_id=shop.id, kind="broadcast", segment="all",
            message_text=text, actor_telegram_id=ctx.actor_telegram_id,
            idempotency_key=f"trial-reset-notify:{ctx.idempotency_key}", customers=customers)
        await session.flush()
        body = {"reset_count": reset_count, "notified": int(job.total_count or 0),
                "job_id": job.id, "period": period}
        return _Mutation(body, {"trial_reset_period": period, **body}, "settings", "trial_reset")

    return await _execute(
        session, shop, ctx, action="trial.reset", command=command,
        before=before, mutate=mutate,
    )


async def _trial_reset_announcement(session: AsyncSession, shop: StorefrontBot) -> str:
    """The owner-editable customer announcement, rendered for this shop.

    Falls back to the registered default when the template has been edited into something with an
    unknown placeholder — a shop's customers getting a raw `{oops}` is worse than losing the
    owner's wording, and a KeyError here would roll back the whole reset.
    """
    tpl = await settings_service.get(session, "tpl_storefront_trial_reset", "") or ""
    fields = {
        "shop": (f"@{shop.bot_username}" if shop.bot_username else "فروشگاه ما"),
        "gb": min(int(shop.free_trial_gb or 1), await storefront.trial_max_gb(session)),
        "days": int(shop.free_trial_days or 1),
    }
    for candidate in (tpl, settings_service.default_for("tpl_storefront_trial_reset") or ""):
        try:
            rendered = candidate.format(**fields).strip()
        except (KeyError, IndexError, ValueError):
            continue
        if rendered:
            return rendered[:4000]
    return "🎁 تست رایگان دوباره برای شما فعال شد."


async def update_messages(
    session: AsyncSession, shop_id: int, ctx: CommandContext, *,
    welcome_text: str | None | object = _UNSET,
    support_contact: str | None | object = _UNSET,
) -> CommandResult:
    if welcome_text is _UNSET and support_contact is _UNSET:
        raise AdminCommandError("validation", "empty message settings")
    intent: dict[str, Any] = {}
    if welcome_text is not _UNSET:
        intent["welcome_text"] = str(welcome_text or "").strip() or None
    if support_contact is not _UNSET:
        intent["support_contact"] = str(support_contact or "").strip() or None
    if len(intent.get("welcome_text") or "") > 1000 or len(
        intent.get("support_contact") or "") > 128:
        raise AdminCommandError("validation", "message/support exceeds maximum length")
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    command, replay = await _claim_db_command(
        session, shop, ctx, action="settings.messages", intent=intent)
    if replay is not None:
        return replay
    before = settings_snapshot(shop)["messages"]
    welcome = intent.get("welcome_text", shop.welcome_text)
    support = intent.get("support_contact", shop.support_contact)
    state = {"welcome_text": welcome, "support_contact": support}

    async def mutate(_version: int) -> _Mutation:
        shop.welcome_text, shop.support_contact = welcome, support
        await session.flush()
        return _Mutation({"messages": state}, state, "settings", "messages")

    return await _execute(
        session, shop, ctx, action="settings.messages", command=command,
        before=before, mutate=mutate,
    )


async def update_shop_state(
    session: AsyncSession, shop_id: int, ctx: CommandContext, *,
    closed: bool | object = _UNSET, closed_text: str | None | object = _UNSET,
) -> CommandResult:
    if closed is _UNSET and closed_text is _UNSET:
        raise AdminCommandError("validation", "empty shop-state settings")
    intent: dict[str, Any] = {}
    if closed is not _UNSET:
        intent["closed"] = bool(closed)
    if closed_text is not _UNSET:
        intent["closed_text"] = str(closed_text or "").strip() or None
    if len(intent.get("closed_text") or "") > 1000:
        raise AdminCommandError("validation", "closed_text exceeds 1000 characters")
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    command, replay = await _claim_db_command(
        session, shop, ctx, action="settings.shop_state", intent=intent)
    if replay is not None:
        return replay
    before = settings_snapshot(shop)["shop_state"]
    desired_closed = bool(intent.get("closed", shop.shop_closed))
    text = intent.get("closed_text", shop.closed_text)
    state = {"closed": desired_closed, "closed_text": text}

    async def mutate(_version: int) -> _Mutation:
        shop.shop_closed, shop.closed_text = desired_closed, text
        await session.flush()
        return _Mutation({"shop_state": state}, state, "settings", "shop_state")

    return await _execute(
        session, shop, ctx, action="settings.shop_state", command=command,
        before=before, mutate=mutate,
    )


async def claim_external_channel(
    session: AsyncSession, shop_id: int, ctx: CommandContext, *, channel_id: str,
) -> ChannelVerification | CommandResult:
    """Persist a leased external command, then commit BEFORE the caller performs Telegram I/O.

    A caller must never repeat invite-link creation after an ``unknown`` result; it must reconcile
    Telegram state or ask the owner to start a new command after inspecting the channel.
    """
    cid = channel_id.strip()
    if not _CHANNEL_ID.fullmatch(cid):
        raise AdminCommandError("validation", "invalid channel id")
    return await _claim_external_channel_action(
        session, shop_id, ctx, action="channel.save", channel_id=cid)


async def claim_external_channel_enable(
    session: AsyncSession, shop_id: int, ctx: CommandContext,
) -> ChannelVerification | CommandResult:
    """Claim a live verification for enabling the already-saved channel, before reading it."""
    return await _claim_external_channel_action(
        session, shop_id, ctx, action="channel.enable", channel_id=None)


async def _claim_external_channel_action(
    session: AsyncSession, shop_id: int, ctx: CommandContext, *,
    action: str, channel_id: str | None,
) -> ChannelVerification | CommandResult:
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    claim = await storefront_audit.claim_command(
        session,
        shop_id=shop.id,
        actor_telegram_id=ctx.actor_telegram_id,
        idempotency_key=ctx.idempotency_key,
        action=action,
        request={"expected_version": ctx.expected_version, "channel_id": channel_id},
        external_io=True,
    )
    if claim.outcome == "conflict":
        raise AdminCommandError("idempotency_conflict", "Idempotency key payload conflict")
    if claim.outcome == "in_flight":
        raise AdminCommandError("in_flight", "Channel verification is already in progress")
    if claim.outcome == "unknown":
        await session.commit()
        raise AdminCommandError("unknown", "Channel verification requires reconciliation")
    if claim.outcome == "replay":
        return _replay_result(claim.command, ctx)
    await storefront_audit.append_event(
        session,
        storefront_bot_id=shop.id, actor_telegram_id=ctx.actor_telegram_id,
        actor_role=await _audit_role(session, shop, ctx), source=ctx.source,
        action=action, outcome="started",
        entity_type="settings", entity_id="channel",
        correlation_id=ctx.correlation_id or ctx.idempotency_key,
    )
    if shop.config_version != ctx.expected_version:
        exc = AdminCommandError(
            "config_conflict", "Configuration changed", current_version=shop.config_version)
        await _cache_known_failure(
            session, shop, ctx, action=action, command=claim.command, exc=exc)
        raise exc
    cid = channel_id or shop.channel_id
    if not cid:
        shop.channel_verification_error = "not_configured"
        exc = AdminCommandError("validation", "channel must be saved before enabling")
        await _cache_known_failure(
            session, shop, ctx, action=action, command=claim.command, exc=exc)
        raise exc
    await session.commit()
    return ChannelVerification(
        command_id=claim.command.id,
        action=action,
        shop_id=shop.id,
        expected_version=ctx.expected_version,
        credential_fingerprint=hashlib.sha256(shop.bot_token_enc.encode()).hexdigest(),
        channel_id=cid,
    )


async def finalize_verified_channel(
    session: AsyncSession, verification: ChannelVerification, ctx: CommandContext, *,
    verified: bool, resolved_link: str | None,
) -> CommandResult:
    if ctx.expected_version != verification.expected_version:
        raise AdminCommandError("config_conflict", "verification version changed")
    link = (resolved_link or "").strip() or None
    shop, _ = await _authorized_shop(
        session, verification.shop_id, ctx.actor_telegram_id, ctx.source)
    command = await session.get(StorefrontApiCommand, verification.command_id)
    if (
        command is None or command.storefront_bot_id != shop.id
        or command.actor_telegram_id != ctx.actor_telegram_id
        or command.idempotency_key != ctx.idempotency_key
        or command.action != verification.action
    ):
        raise AdminCommandError("unknown", "External command record is unavailable")
    if command.status in ("succeeded", "failed"):
        return _replay_result(command, ctx)
    now = dt.datetime.now(dt.timezone.utc)
    if (
        command.status != "pending" or command.lease_expires_at is None
        or _as_utc(command.lease_expires_at) <= now
    ):
        command.status = "unknown"
        command.lease_expires_at = None
        command.error_class = "lease_expired_external"
        shop.channel_verification_error = "unknown"
        await session.commit()
        raise AdminCommandError("unknown", "Channel verification requires reconciliation")
    if link is not None and (len(link) > 255 or not link.startswith("https://t.me/")):
        exc = AdminCommandError("validation", "invalid server-resolved channel link")
        await _cache_known_failure(
            session, shop, ctx, action=verification.action, command=command, exc=exc)
        raise exc
    fingerprint = hashlib.sha256(shop.bot_token_enc.encode()).hexdigest()
    before = settings_snapshot(shop)["channel"]
    if fingerprint != verification.credential_fingerprint:
        shop.channel_verification_error = "credential_changed"
        body = {"error": "config_conflict", "config_version": shop.config_version}
        await storefront_audit.finalize_command(
            session, command, succeeded=False, response_status=409, response_body=body,
            error_class="credential_changed",
        )
        await storefront_audit.append_event(
            session,
            storefront_bot_id=shop.id, actor_telegram_id=ctx.actor_telegram_id,
            actor_role=await _audit_role(session, shop, ctx), source=ctx.source,
            action=verification.action,
            outcome="conflict", entity_type="settings", entity_id="channel",
            correlation_id=ctx.correlation_id or ctx.idempotency_key,
            before=before, error_class="credential_changed",
        )
        await session.commit()
        raise AdminCommandError(
            "config_conflict", "shop bot credential changed", current_version=shop.config_version,
            response_status=409, response_body=body,
        )
    if not verified:
        shop.channel_verification_error = "external_failure"
        body = {"error": "channel_verification_failed", "config_version": shop.config_version}
        await storefront_audit.append_event(
            session,
            storefront_bot_id=shop.id,
            actor_telegram_id=ctx.actor_telegram_id,
            actor_role=await _audit_role(session, shop, ctx),
            source=ctx.source,
            action=verification.action,
            outcome="failed",
            entity_type="settings",
            entity_id="channel",
            correlation_id=ctx.correlation_id or ctx.idempotency_key,
            before=before,
            error_class="external_failure",
        )
        await storefront_audit.finalize_command(
            session, command, succeeded=False, response_status=502,
            response_body=body, error_class="external_failure",
        )
        await session.commit()
        return CommandResult(502, body, shop.config_version)

    effective_link = link if link is not None else (
        shop.channel_link if verification.action == "channel.enable" else None)
    state = {
        "required": True, "channel_id": verification.channel_id,
        "channel_link": effective_link,
    }
    new_version = await _cas(session, shop, verification.expected_version)
    if new_version is None:
        current = await _current_version(session, shop.id)
        body = {"error": "config_conflict", "config_version": current}
        await storefront_audit.finalize_command(
            session, command, succeeded=False, response_status=409, response_body=body,
            error_class="config_conflict",
        )
        await storefront_audit.append_event(
            session,
            storefront_bot_id=shop.id, actor_telegram_id=ctx.actor_telegram_id,
            actor_role=await _audit_role(session, shop, ctx), source=ctx.source,
            action=verification.action,
            outcome="conflict", entity_type="settings", entity_id="channel",
            correlation_id=ctx.correlation_id or ctx.idempotency_key,
            before=before, error_class="config_conflict",
        )
        await session.commit()
        raise AdminCommandError(
            "config_conflict", "Configuration changed", current_version=current,
            response_status=409, response_body=body,
        )
    shop.channel_required = True
    shop.channel_id, shop.channel_link = verification.channel_id, effective_link
    shop.channel_verified_at = dt.datetime.now(dt.timezone.utc)
    shop.channel_verification_error = None
    response = storefront_audit.safe_cached_response({
        "channel": {
            "required": True, "channel_id": verification.channel_id,
            "has_link": bool(effective_link), "verified_at": shop.channel_verified_at,
        },
        "config_version": new_version,
    })
    await storefront_audit.append_event(
        session,
        storefront_bot_id=shop.id, actor_telegram_id=ctx.actor_telegram_id,
        actor_role=await _audit_role(session, shop, ctx), source=ctx.source,
        action=verification.action,
        outcome="succeeded", entity_type="settings", entity_id="channel",
        correlation_id=ctx.correlation_id or ctx.idempotency_key, before=before, after=state,
    )
    await storefront_audit.finalize_command(
        session, command, succeeded=True, response_status=200, response_body=response,
    )
    await session.commit()
    return CommandResult(200, response, new_version)


async def mark_external_channel_unknown(
    session: AsyncSession,
    verification: ChannelVerification,
    ctx: CommandContext,
    *,
    error_class: str = "external_unknown",
) -> CommandResult:
    """Persist an ambiguous Telegram outcome without retrying the leased external command."""
    shop, _ = await _authorized_shop(
        session, verification.shop_id, ctx.actor_telegram_id, ctx.source)
    command = await session.get(StorefrontApiCommand, verification.command_id)
    if (
        command is None or command.storefront_bot_id != shop.id
        or command.actor_telegram_id != ctx.actor_telegram_id
        or command.idempotency_key != ctx.idempotency_key
        or command.action != verification.action
    ):
        raise AdminCommandError("unknown", "External command record is unavailable")
    if command.status in ("succeeded", "failed"):
        return _replay_result(command, ctx)
    if command.status == "unknown":
        await session.commit()
        raise AdminCommandError("unknown", "Channel verification requires reconciliation")
    before = settings_snapshot(shop)["channel"]
    command.status = "unknown"
    command.lease_expires_at = None
    command.error_class = error_class[:32]
    command.response_status = 502
    command.response_body = storefront_audit.safe_cached_response({
        "error": error_class,
        "config_version": shop.config_version,
    })
    shop.channel_verification_error = error_class
    await storefront_audit.append_event(
        session,
        storefront_bot_id=shop.id,
        actor_telegram_id=ctx.actor_telegram_id,
        actor_role=await _audit_role(session, shop, ctx),
        source=ctx.source,
        action=verification.action,
        outcome="unknown",
        entity_type="settings",
        entity_id="channel",
        correlation_id=ctx.correlation_id or ctx.idempotency_key,
        before=before,
        error_class=error_class,
    )
    await session.commit()
    return CommandResult(
        502,
        {"error": error_class, "config_version": shop.config_version},
        shop.config_version,
    )


async def set_channel_required(
    session: AsyncSession, shop_id: int, ctx: CommandContext, *, required: bool,
) -> CommandResult:
    """Absolute enable/disable; enabling is allowed only for a previously verified channel."""
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    command, replay = await _claim_db_command(
        session, shop, ctx, action="channel.set_required", intent={"required": bool(required)})
    if replay is not None:
        return replay
    if required and (
        not shop.channel_id or shop.channel_verified_at is None or shop.channel_verification_error
    ):
        exc = AdminCommandError("validation", "channel must be verified before enabling")
        await _cache_known_failure(
            session, shop, ctx, action="channel.set_required", command=command, exc=exc)
        raise exc
    before = settings_snapshot(shop)["channel"]
    state = {**before, "required": bool(required)}

    async def mutate(_version: int) -> _Mutation:
        shop.channel_required = bool(required)
        await session.flush()
        return _Mutation({"channel": state}, state, "settings", "channel")

    return await _execute(
        session, shop, ctx, action="channel.set_required",
        command=command, before=before, mutate=mutate,
    )


async def delete_channel(
    session: AsyncSession, shop_id: int, ctx: CommandContext,
) -> CommandResult:
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    command, replay = await _claim_db_command(
        session, shop, ctx, action="channel.delete", intent={})
    if replay is not None:
        return replay
    before = settings_snapshot(shop)["channel"]
    state = {"required": False, "channel_id": None, "channel_link": None}

    async def mutate(_version: int) -> _Mutation:
        shop.channel_required, shop.channel_id, shop.channel_link = False, None, None
        shop.channel_verified_at, shop.channel_verification_error = None, None
        await session.flush()
        return _Mutation({"channel": state}, state, "settings", "channel")

    return await _execute(
        session, shop, ctx, action="channel.delete", command=command,
        before=before, mutate=mutate,
    )


async def list_managers(
    session: AsyncSession, shop_id: int, actor_id: int, source: Source,
) -> dict:
    shop, reseller = await _authorized_shop(
        session, shop_id, actor_id, source, owner_only=True)
    return {
        "config_version": shop.config_version,
        "owner_telegram_id": reseller.bot_chat_id,
        "co_admin_ids": storefront.co_admin_ids(shop),
    }


async def add_manager(
    session: AsyncSession, shop_id: int, ctx: CommandContext, *, telegram_id: int,
) -> CommandResult:
    if not 0 < telegram_id <= _MAX_I64:
        raise AdminCommandError("validation", "manager Telegram id is invalid")
    shop, reseller = await _authorized_shop(
        session, shop_id, ctx.actor_telegram_id, ctx.source, owner_only=True)
    command, replay = await _claim_db_command(
        session, shop, ctx, action="manager.add", intent={"telegram_id": telegram_id})
    if replay is not None:
        return replay
    try:
        if reseller.bot_chat_id == telegram_id:
            raise AdminCommandError("validation", "owner is already a manager")
        managers = storefront.co_admin_ids(shop)
        if telegram_id not in managers and len(managers) >= storefront.MAX_CO_ADMINS:
            raise AdminCommandError("validation", "manager limit reached")
        banned = await session.scalar(
            select(StorefrontCustomer.id).where(
                StorefrontCustomer.storefront_bot_id == shop.id,
                StorefrontCustomer.telegram_id == telegram_id,
                StorefrontCustomer.banned.is_(True),
            ).limit(1)
        )
        if banned is not None:
            raise AdminCommandError("validation", "a banned customer cannot be a manager")
    except AdminCommandError as exc:
        await _cache_known_failure(
            session, shop, ctx, action="manager.add", command=command, exc=exc)
        raise
    before = {"co_admin_ids": managers}
    desired = [*managers] if telegram_id in managers else [*managers, telegram_id]

    async def mutate(_version: int) -> _Mutation:
        shop.co_admin_ids = ",".join(str(item) for item in desired) or None
        await session.flush()
        state = {"co_admin_ids": desired}
        return _Mutation({"managers": state}, state, "manager", telegram_id)

    return await _execute(
        session, shop, ctx, action="manager.add", command=command,
        before=before, mutate=mutate,
    )


async def remove_manager(
    session: AsyncSession, shop_id: int, ctx: CommandContext, *, telegram_id: int,
) -> CommandResult:
    if not 0 < telegram_id <= _MAX_I64:
        raise AdminCommandError("validation", "manager Telegram id is invalid")
    shop, _ = await _authorized_shop(
        session, shop_id, ctx.actor_telegram_id, ctx.source, owner_only=True)
    command, replay = await _claim_db_command(
        session, shop, ctx, action="manager.remove", intent={"telegram_id": telegram_id})
    if replay is not None:
        return replay
    managers = storefront.co_admin_ids(shop)
    desired = [item for item in managers if item != telegram_id]
    before = {"co_admin_ids": managers}

    async def mutate(_version: int) -> _Mutation:
        shop.co_admin_ids = ",".join(str(item) for item in desired) or None
        await session.flush()
        state = {"co_admin_ids": desired}
        return _Mutation({"managers": state}, state, "manager", telegram_id)

    return await _execute(
        session, shop, ctx, action="manager.remove", command=command,
        before=before, mutate=mutate,
    )


async def customer_preview(
    session: AsyncSession, shop_id: int, actor_id: int, source: Source,
) -> dict:
    """Pure presentation DTO: deliberately never resolves or creates a customer."""
    shop, _ = await _authorized_shop(session, shop_id, actor_id, source)
    plans = await storefront.list_plans(session, shop.id, only_enabled=True)
    return {
        "config_version": shop.config_version,
        "welcome_text": shop.welcome_text or "🛍 به فروشگاهِ ما خوش آمدید!",
        "support_contact": shop.support_contact,
        "channel_required": bool(shop.channel_required and shop.channel_id),
        "shop_closed": shop.shop_closed,
        "closed_text": shop.closed_text,
        "trial": {
            "enabled": shop.free_trial_enabled, "gb": shop.free_trial_gb,
            "days": shop.free_trial_days,
        },
        "payment_methods": {
            "card": bool(shop.pay_card_enabled and shop.card_number),
            "usdt": bool(shop.pay_usdt_enabled and shop.usdt_address),
            "ton": bool(shop.pay_ton_enabled and shop.ton_address),
        },
        "plans": [_plan_dict(plan) for plan in plans],
    }


# ── customer & order commands (plan 004) ─────────────────────────────────────
# Customer ban and order enable/delete/renew are ENTITY-scoped, not shop-config-scoped: running
# them through the config_version CAS would spuriously 409 against a concurrent plan/settings edit.
# So they use idempotency + audit WITHOUT the version bump (`_execute_entity`) and require only an
# Idempotency-Key, not If-Match. Order enable/delete are durable external-I/O commands (started →
# release DB → panel I/O → finalize/unknown), reconciled by the reaper on an ambiguous result.


async def _owned_customer(
    session: AsyncSession, shop_id: int, customer_id: int
) -> StorefrontCustomer:
    cust = await session.get(StorefrontCustomer, customer_id)
    if cust is None or cust.storefront_bot_id != shop_id:
        raise AdminCommandError("not_found", "Customer not found")
    return cust


async def _owned_order(
    session: AsyncSession, shop_id: int, order_id: int, *, customer_id: int | None = None
) -> StorefrontOrder:
    order = await session.get(StorefrontOrder, order_id)
    if order is None:
        raise AdminCommandError("not_found", "Order not found")
    cust = await session.get(StorefrontCustomer, order.customer_id)
    if cust is None or cust.storefront_bot_id != shop_id or (
        customer_id is not None and cust.id != customer_id
    ):
        raise AdminCommandError("not_found", "Order not found")
    return order


async def _claim_entity_command(
    session: AsyncSession, shop: StorefrontBot, ctx: CommandContext, *,
    action: str, intent: dict, external_io: bool = False,
) -> tuple[StorefrontApiCommand, CommandResult | None]:
    """Claim an entity command keyed on client intent ONLY (no config_version), so a same-key retry
    replays cleanly and never conflicts with a concurrent shop-config edit."""
    claim = await storefront_audit.claim_command(
        session,
        shop_id=shop.id,
        actor_telegram_id=ctx.actor_telegram_id,
        idempotency_key=ctx.idempotency_key,
        action=action,
        request=dict(intent),
        external_io=external_io,
    )
    if claim.outcome == "conflict":
        raise AdminCommandError("idempotency_conflict", "Idempotency key payload conflict")
    if claim.outcome == "in_flight":
        raise AdminCommandError("in_flight", "Command is already in progress")
    if claim.outcome == "unknown":
        await session.commit()
        raise AdminCommandError("unknown", "Command outcome requires reconciliation")
    if claim.outcome == "replay":
        return claim.command, _replay_result(claim.command, ctx)
    return claim.command, None


async def _execute_entity(
    session: AsyncSession, shop: StorefrontBot, ctx: CommandContext, *,
    action: str, command: StorefrontApiCommand,
    before: dict | list | None, mutate: Callable[[], Awaitable[_Mutation]],
) -> CommandResult:
    """Like `_execute` but WITHOUT the config_version CAS (entity-scoped write)."""
    try:
        current = await _current_version(session, shop.id)
        mutation = await mutate()
        body: dict | list | None = mutation.body
        if isinstance(body, dict):
            body = {**body, "config_version": current}
        body = storefront_audit.safe_cached_response(body)
        await storefront_audit.append_event(
            session, storefront_bot_id=shop.id, actor_telegram_id=ctx.actor_telegram_id,
            actor_role=await _audit_role(session, shop, ctx), source=ctx.source,
            action=action, outcome="succeeded", entity_type=mutation.entity_type,
            entity_id=mutation.entity_id, correlation_id=ctx.correlation_id or ctx.idempotency_key,
            before=before, after=mutation.after,
        )
        await storefront_audit.finalize_command(
            session, command, succeeded=True, response_status=mutation.response_status,
            response_body=body,
        )
        await session.commit()
        return CommandResult(mutation.response_status, body, current)
    except Exception:
        await session.rollback()
        raise


async def set_customer_banned(
    session: AsyncSession, shop_id: int, customer_id: int, ctx: CommandContext, *,
    banned: bool, reason: str,
) -> CommandResult:
    """Set a customer's ban flag to an ABSOLUTE value with a mandatory audited reason."""
    reason = (reason or "").strip()
    if not 3 <= len(reason) <= 255:
        raise AdminCommandError("validation", "reason must be 3..255 characters")
    # Customer ban is an admin action (owner OR a bot co-admin), unlike owner-only manager management.
    shop, _ = await _authorized_shop(
        session, shop_id, ctx.actor_telegram_id, ctx.source)
    intent = {"customer_id": int(customer_id), "banned": bool(banned), "reason": reason}
    command, replay = await _claim_entity_command(
        session, shop, ctx, action="customer.ban", intent=intent)
    if replay is not None:
        return replay
    try:
        cust = await _owned_customer(session, shop_id, customer_id)
    except AdminCommandError as exc:
        await _cache_known_failure(
            session, shop, ctx, action="customer.ban", command=command, exc=exc)
        raise
    before = {"banned": cust.banned}

    async def mutate() -> _Mutation:
        cust.banned = bool(banned)
        await session.flush()
        state = {"banned": bool(banned), "reason": reason}
        return _Mutation({"customer": {"id": cust.id, "banned": bool(banned)}}, state,
                         "customer", cust.id)

    return await _execute_entity(
        session, shop, ctx, action="customer.ban", command=command, before=before, mutate=mutate)


async def _finalize_order_command(
    session: AsyncSession, shop: StorefrontBot, ctx: CommandContext, *,
    action: str, command: StorefrontApiCommand, order_id: int,
    before: dict, result, is_delete: bool,  # noqa: ANN001
) -> CommandResult:
    from app.services.storefront_subscription import SubResult
    assert isinstance(result, SubResult)
    role = await _audit_role(session, shop, ctx)
    # Success (including an idempotent delete of an already-deleted order).
    if result.ok or (is_delete and result.reason == "not_found"):
        order = await session.get(StorefrontOrder, order_id)
        status = order.status if order is not None else ("deleted" if is_delete else "unknown")
        body = storefront_audit.safe_cached_response({"order": {"id": order_id, "status": status}})
        await storefront_audit.append_event(
            session, storefront_bot_id=shop.id, actor_telegram_id=ctx.actor_telegram_id,
            actor_role=role, source=ctx.source, action=action, outcome="succeeded",
            entity_type="order", entity_id=order_id,
            correlation_id=ctx.correlation_id or ctx.idempotency_key,
            before=before, after={"status": status},
        )
        await storefront_audit.finalize_command(
            session, command, succeeded=True, response_status=200, response_body=body)
        await session.commit()
        return CommandResult(200, body, await _current_version(session, shop.id))
    # Definite validation failure (order not in an actionable state). `below_cost` belongs here and
    # NOT in the ambiguous branch below: it is decided before any durable state or panel I/O, so
    # there is nothing for the reaper to reconcile.
    if result.reason in ("not_found", "trial", "below_cost"):
        body = {"error": result.reason, "order_id": order_id}
        await storefront_audit.append_event(
            session, storefront_bot_id=shop.id, actor_telegram_id=ctx.actor_telegram_id,
            actor_role=role, source=ctx.source, action=action, outcome="failed",
            entity_type="order", entity_id=order_id,
            correlation_id=ctx.correlation_id or ctx.idempotency_key,
            before=before, error_class=result.reason,
        )
        await storefront_audit.finalize_command(
            session, command, succeeded=False, response_status=422, response_body=body,
            error_class=result.reason)
        await session.commit()
        raise AdminCommandError(
            "validation", result.message or result.reason, response_status=422, response_body=body)
    # Ambiguous panel result → never a false success. Mark unknown; the reaper reconciles by an
    # idempotent absolute re-apply (enable/delete are absolute, so a re-apply is a no-op if it landed).
    command.status = "unknown"
    command.lease_expires_at = None
    command.error_class = "external_unknown"
    await storefront_audit.append_event(
        session, storefront_bot_id=shop.id, actor_telegram_id=ctx.actor_telegram_id,
        actor_role=role, source=ctx.source, action=action, outcome="unknown",
        entity_type="order", entity_id=order_id,
        correlation_id=ctx.correlation_id or ctx.idempotency_key,
        before=before, error_class="external_unknown",
    )
    await session.commit()
    raise AdminCommandError(
        "external_unknown", "Panel result uncertain; will reconcile",
        response_status=502, response_body={"error": "external_unknown", "order_id": order_id})


async def _external_order_command(
    session: AsyncSession, shop_id: int, ctx: CommandContext, *,
    action: str, order_id: int, intent: dict, io_call: Callable[[], Awaitable], is_delete: bool = False,
) -> CommandResult:
    shop, _ = await _authorized_shop(
        session, shop_id, ctx.actor_telegram_id, ctx.source, owner_only=True)
    command, replay = await _claim_entity_command(
        session, shop, ctx, action=action, intent=intent, external_io=True)
    if replay is not None:
        return replay
    try:
        order = await _owned_order(session, shop_id, order_id)
    except AdminCommandError as exc:
        await _cache_known_failure(session, shop, ctx, action=action, command=command, exc=exc)
        raise
    before = {"status": order.status}
    await storefront_audit.append_event(
        session, storefront_bot_id=shop.id, actor_telegram_id=ctx.actor_telegram_id,
        actor_role=await _audit_role(session, shop, ctx), source=ctx.source,
        action=action, outcome="started", entity_type="order", entity_id=order_id,
        correlation_id=ctx.correlation_id or ctx.idempotency_key, before=before,
    )
    await session.commit()  # release the DB transaction BEFORE the panel network call
    result = await io_call()
    return await _finalize_order_command(
        session, shop, ctx, action=action, command=command, order_id=order_id,
        before=before, result=result, is_delete=is_delete)


async def set_order_enabled(
    session: AsyncSession, shop_id: int, order_id: int, ctx: CommandContext, *, enabled: bool,
) -> CommandResult:
    """Pause/resume (absolute) a customer's service. Idempotent external-I/O command."""
    from app.services import storefront_subscription
    return await _external_order_command(
        session, shop_id, ctx, action="order.set_enabled", order_id=order_id,
        intent={"order_id": int(order_id), "enabled": bool(enabled)},
        io_call=lambda: storefront_subscription.set_enabled(
            SessionLocal, order_id=order_id, enabled=bool(enabled), expected_sf_id=shop_id),
    )


async def delete_order(
    session: AsyncSession, shop_id: int, order_id: int, ctx: CommandContext, *, reason: str,
) -> CommandResult:
    """Delete a customer's service (no refund). Idempotent external-I/O command."""
    from app.services import storefront_subscription
    reason = (reason or "").strip()
    if not 3 <= len(reason) <= 255:
        raise AdminCommandError("validation", "reason must be 3..255 characters")
    return await _external_order_command(
        session, shop_id, ctx, action="order.delete", order_id=order_id,
        intent={"order_id": int(order_id), "reason": reason},
        io_call=lambda: storefront_subscription.delete_subscription(
            SessionLocal, order_id=order_id, expected_sf_id=shop_id),
        is_delete=True,
    )


async def renew_order(
    session: AsyncSession, shop_id: int, order_id: int, ctx: CommandContext,
) -> CommandResult:
    """Free admin renewal of a customer's service. Idempotency + crash safety are provided by the
    money-layer StorefrontOperation: the op_id is derived from the browser Idempotency-Key so a retry
    REPLAYS the same operation (no second grant). Tenant + actor authorization is enforced here."""
    import hashlib as _hashlib

    from app.core.config import settings as _settings
    from app.services import storefront_subscription
    shop, _ = await _authorized_shop(
        session, shop_id, ctx.actor_telegram_id, ctx.source, owner_only=True)
    await _owned_order(session, shop_id, order_id)
    op_id = _hashlib.sha256(
        f"{_settings.secret_key}|sf-renew:{shop_id}:{order_id}:{ctx.idempotency_key}".encode()
    ).hexdigest()[:32]
    result = await storefront_subscription.renew(
        SessionLocal, order_id=order_id, by_admin=True, op_id=op_id, expected_sf_id=shop_id)
    current = await _current_version(session, shop_id)
    role = await _audit_role(session, shop, ctx)
    if result.ok:
        body = storefront_audit.safe_cached_response(
            {"order": {"id": order_id, "renewed": True, "gb": result.gb, "days": result.days},
             "config_version": current})
        await storefront_audit.append_event(
            session, storefront_bot_id=shop.id, actor_telegram_id=ctx.actor_telegram_id,
            actor_role=role, source=ctx.source, action="order.renew", outcome="succeeded",
            entity_type="order", entity_id=order_id,
            correlation_id=ctx.correlation_id or ctx.idempotency_key,
            after={"gb": result.gb, "days": result.days})
        await session.commit()
        return CommandResult(200, body, current)
    reason = result.reason or "error"
    # `below_cost` is a deterministic pre-flight refusal (no durable state, no panel call), so it
    # must not be reported as an external failure the reaper would try to reconcile.
    outcome, code, status = (
        ("failed", "validation", 422) if reason in ("not_found", "trial", "below_cost")
        else ("failed", "in_flight", 409) if reason == "processing"
        else ("failed", "external_failure", 502))
    await storefront_audit.append_event(
        session, storefront_bot_id=shop.id, actor_telegram_id=ctx.actor_telegram_id,
        actor_role=role, source=ctx.source, action="order.renew", outcome=outcome,
        entity_type="order", entity_id=order_id,
        correlation_id=ctx.correlation_id or ctx.idempotency_key, error_class=reason)
    await session.commit()
    raise AdminCommandError(
        code, result.message or reason, response_status=status,
        response_body={"error": reason, "order_id": order_id})


# ── wallet & top-up money commands (plan 005) ────────────────────────────────
# Money decisions (top-up confirm/reject, manual wallet adjust, bulk) are ENTITY commands
# (external_io=False, Idempotency-Key only). They wrap the existing row-locked wallet cores
# (storefront_wallet._confirm_topup_core/_reject_topup_core/_manual_adjust_core) so the money write,
# the audit event and the idempotency finalize all COMMIT in ONE transaction (`_execute_entity`).
# The wallet cores' FOR-UPDATE lock + status re-check remain the exactly-once money authority.


async def _owned_topup(
    session: AsyncSession, shop_id: int, txn_id: int
) -> StorefrontWalletTxn:
    txn = await session.get(StorefrontWalletTxn, txn_id)
    if txn is None or txn.storefront_bot_id != shop_id or txn.kind != "topup":
        raise AdminCommandError("not_found", "Top-up not found")
    return txn


async def adjust_wallet(
    session: AsyncSession, shop_id: int, customer_id: int, ctx: CommandContext, *,
    amount_toman_signed: int, reason: str,
) -> CommandResult:
    """Manually credit (+) or debit (−) a customer's wallet. Overdraw is clamped to zero, so the
    APPLIED delta can differ from the requested — both are reported. Idempotent + audited."""
    from app.services import storefront_wallet
    if int(amount_toman_signed) == 0:
        raise AdminCommandError("validation", "amount must be non-zero")
    reason = (reason or "").strip()
    if not 3 <= len(reason) <= 255:
        raise AdminCommandError("validation", "reason must be 3..255 characters")
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    intent = {"customer_id": int(customer_id), "amount_toman_signed": int(amount_toman_signed),
              "reason": reason}
    command, replay = await _claim_entity_command(
        session, shop, ctx, action="wallet.adjust", intent=intent)
    if replay is not None:
        return replay
    try:
        cust = await _owned_customer(session, shop_id, customer_id)
    except AdminCommandError as exc:
        await _cache_known_failure(
            session, shop, ctx, action="wallet.adjust", command=command, exc=exc)
        raise
    old_balance = int(storefront_wallet.balance(cust))

    async def mutate() -> _Mutation:
        txn = await storefront_wallet._manual_adjust_core(
            session, cust, int(amount_toman_signed), note=reason)
        applied = int(Decimal(str(txn.amount_toman)))
        new_balance = int(storefront_wallet.balance(cust))
        # Derive old from new−applied so it's accurate even if a concurrent adjust moved the balance
        # between our pre-lock read and the locked mutation (the row lock refreshed it inside the core).
        body = {"ledger_id": txn.id, "requested_delta": int(amount_toman_signed),
                "applied_delta": applied, "old_balance": new_balance - applied,
                "new_balance": new_balance}
        return _Mutation(body, body, "wallet", customer_id)

    return await _execute_entity(
        session, shop, ctx, action="wallet.adjust", command=command,
        before={"balance": old_balance}, mutate=mutate)


async def set_topup_decision(
    session: AsyncSession, shop_id: int, txn_id: int, ctx: CommandContext, *,
    decision: Literal["confirm", "reject"], corrected_amount: int | None = None, reason: str = "",
) -> CommandResult:
    """Confirm or reject a pending top-up (the row-locked wallet core is the money authority). An
    optional corrected credited amount is allowed only on confirm; a reason is mandatory for a
    rejection or a correction. Idempotent (a re-decided top-up is a no-op `already_decided`)."""
    from app.services import storefront_wallet
    if decision not in ("confirm", "reject"):
        raise AdminCommandError("validation", "decision must be confirm or reject")
    reason = (reason or "").strip()
    if corrected_amount is not None:
        if decision != "confirm":
            raise AdminCommandError("validation", "corrected_amount is only allowed with confirm")
        if not 1 <= int(corrected_amount) <= 10**12:
            raise AdminCommandError("validation", "corrected_amount out of range")
    if (decision == "reject" or corrected_amount is not None) and not 3 <= len(reason) <= 255:
        raise AdminCommandError("validation", "reason must be 3..255 characters")
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    intent = {"txn_id": int(txn_id), "decision": decision,
              "corrected_amount": int(corrected_amount) if corrected_amount is not None else None,
              "reason": reason}
    command, replay = await _claim_entity_command(
        session, shop, ctx, action="topup.decide", intent=intent)
    if replay is not None:
        return replay
    try:
        txn0 = await _owned_topup(session, shop_id, txn_id)
    except AdminCommandError as exc:
        await _cache_known_failure(
            session, shop, ctx, action="topup.decide", command=command, exc=exc)
        raise
    before = {"status": txn0.status, "amount_toman": int(Decimal(str(txn0.amount_toman or 0)))}

    async def mutate() -> _Mutation:
        credit_q = None
        if decision == "confirm":
            changed, t, credit_q = await storefront_wallet._confirm_topup_core(
                session, txn_id, expected_storefront_bot_id=shop.id, amount_toman=corrected_amount)
        else:
            changed, t = await storefront_wallet._reject_topup_core(
                session, txn_id, expected_storefront_bot_id=shop.id)
        credited = (
            int(Decimal(str(t.amount_toman))) if (t is not None and decision == "confirm") else None)
        # Record the captured-code outcome so an archived/expired/no-bonus code is auditable (a
        # committed decision never silently drops the bonus it did or didn't grant).
        code_applied = bool(credit_q and credit_q.ok and credit_q.bonus_toman > 0)
        credit_bonus = int(credit_q.bonus_toman) if code_applied and credit_q else 0
        code_no_bonus_reason = (
            credit_q.reason if (credit_q is not None and not credit_q.ok) else None)
        body = {
            "txn_id": int(txn_id), "decision": decision, "changed": bool(changed),
            "already_decided": (not changed), "credited": credited,
            "requested": (int(Decimal(str(t.requested_amount_toman)))
                          if (t is not None and t.requested_amount_toman is not None) else None),
            "status": t.status if t is not None else None,
            "credit_applied": code_applied, "credit_bonus_toman": credit_bonus,
            "credit_no_bonus_reason": code_no_bonus_reason,
        }
        # Durable customer notification (portal source): enqueue a kind='direct' delivery in the SAME
        # transaction as the money decision, so it can never fire for a rolled-back decision and a
        # committed decision can never lose its notification. `source='system'` bypasses the direct
        # rate gate; the worker delivers it with flood control. (The bot source notifies inline.)
        if changed and ctx.source == "portal" and t is not None:
            from app.services import storefront_delivery
            cust = await session.get(StorefrontCustomer, t.customer_id)
            if cust is not None and cust.telegram_id:
                if decision == "confirm":
                    bonus_note = (f" (+{credit_bonus:,} تومان پاداشِ کدِ شارژ)"
                                  if code_applied else "")
                    note = (f"✅ شارژِ کیفِ پولِ شما به مبلغِ {int(credited or 0):,} تومان "
                            f"تأیید شد.{bonus_note}")
                else:
                    note = ("❌ درخواستِ شارژِ کیفِ پولِ شما تأیید نشد. "
                            "در صورتِ نیاز می‌توانید دوباره اقدام کنید یا با پشتیبانی در تماس باشید.")
                await storefront_delivery.snapshot_job(
                    session, storefront_bot_id=shop.id, kind="direct", segment=None,
                    message_text=note, actor_telegram_id=ctx.actor_telegram_id,
                    idempotency_key=f"topup-notify:{ctx.idempotency_key}", customers=[cust])
        return _Mutation(body, body, "wallet_txn", txn_id)

    return await _execute_entity(
        session, shop, ctx, action="topup.decide", command=command, before=before, mutate=mutate)


async def bulk_topup_decisions(
    session: AsyncSession, shop_id: int, ctx: CommandContext, *,
    items: list[dict], reason: str = "",
) -> CommandResult:
    """Decide up to 100 top-ups in one request. The PARENT idempotency key covers the whole item
    set; each item runs a full `set_topup_decision` under a DETERMINISTIC child key so each commits
    independently and a crashed batch replays completed children + continues the rest. NEVER
    all-or-nothing: an already-committed child is never rolled back."""
    if not items or len(items) > 100:
        raise AdminCommandError("validation", "items must be 1..100")
    seen: set[int] = set()
    norm: list[dict] = []
    for it in items:
        try:
            tid = int(it["txn_id"])
            dec = str(it["decision"]).strip().lower()
        except (KeyError, TypeError, ValueError) as exc:
            raise AdminCommandError("validation", "each item needs txn_id + decision") from exc
        if dec not in ("confirm", "reject"):
            raise AdminCommandError("validation", "decision must be confirm or reject")
        if tid in seen:
            raise AdminCommandError("validation", "duplicate txn_id in batch")
        seen.add(tid)
        norm.append({"txn_id": tid, "decision": dec})
    reason = (reason or "").strip()
    if any(i["decision"] == "reject" for i in norm) and not 3 <= len(reason) <= 255:
        raise AdminCommandError("validation", "reason 3..255 required when the batch has a reject")
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    parent_req = {"items": sorted((i["txn_id"], i["decision"]) for i in norm), "reason": reason}
    parent = await storefront_audit.claim_command(
        session, shop_id=shop.id, actor_telegram_id=ctx.actor_telegram_id,
        idempotency_key=ctx.idempotency_key, action="topup.bulk_decide", request=parent_req)
    if parent.outcome == "conflict":
        raise AdminCommandError("idempotency_conflict", "bulk key payload conflict")
    if parent.outcome == "in_flight":
        raise AdminCommandError("in_flight", "a bulk decision is already in progress")
    if parent.outcome == "replay":
        return _replay_result(parent.command, ctx)
    parent_command_id = parent.command.id
    await session.commit()  # make the parent claim durable before running children

    counts = {"changed": 0, "already_decided": 0, "not_found": 0, "failed": 0}
    results: list[dict] = []
    parent_key = ctx.idempotency_key
    for i in norm:
        tid, dec = i["txn_id"], i["decision"]
        child_key = hashlib.sha256(f"{parent_key}|{tid}|{dec}".encode()).hexdigest()
        child_ctx = CommandContext(
            actor_telegram_id=ctx.actor_telegram_id, actor_role=ctx.actor_role, source=ctx.source,
            idempotency_key=child_key, expected_version=1, correlation_id=ctx.correlation_id or parent_key)
        try:
            child = await set_topup_decision(
                session, shop_id, tid, child_ctx,
                decision=dec, reason=(reason if dec == "reject" else ""))
            body = child.body if isinstance(child.body, dict) else {}
            res = "changed" if body.get("changed") else "already_decided"
        except AdminCommandError as exc:
            res = "not_found" if exc.code == "not_found" else "failed"
        except Exception:  # noqa: BLE001 — one bad item must not abort the batch
            await session.rollback()
            log.warning("bulk top-up child failed", exc_info=True)
            res = "failed"
        results.append({"txn_id": tid, "result": res})
        counts[res] = counts.get(res, 0) + 1

    agg = storefront_audit.safe_cached_response({"results": results, "counts": counts})
    parent_command = await session.get(StorefrontApiCommand, parent_command_id)
    if parent_command is not None:
        await storefront_audit.finalize_command(
            session, parent_command, succeeded=True, response_status=200, response_body=agg)
    await session.commit()
    return CommandResult(200, agg, await _current_version(session, shop_id))


# ── credit codes («کد شارژ/هدیه») ──────────────────────────────────────────────
# History-preserving credit-code management on the shared audited entity-command layer. The
# redemption authority (reserve/record/quote, FOR UPDATE on the code row) stays in
# `storefront_credit`; these commands only manage the code definition (create/edit/enable/archive).

_CREDIT_KINDS = ("percent", "fixed")
# Fields a caller may PATCH; `code`/`code_ci` are immutable (the redemption ledger keys on the code).
_CREDIT_EDITABLE = frozenset({
    "kind", "percent_off", "amount_toman", "max_bonus_toman", "min_topup_toman", "is_gift",
    "max_uses", "per_customer_limit", "starts_at", "expires_at", "enabled",
})
# After ANY redemption, only these may still change (economics are frozen once money has moved).
_CREDIT_POST_REDEMPTION = frozenset({"enabled", "expires_at"})


def _credit_dict(c: StorefrontCreditCode) -> dict:
    return {
        "id": c.id, "code": c.code, "kind": c.kind, "percent_off": c.percent_off,
        "amount_toman": c.amount_toman, "max_bonus_toman": c.max_bonus_toman,
        "min_topup_toman": c.min_topup_toman, "is_gift": bool(c.is_gift),
        "max_uses": c.max_uses, "per_customer_limit": c.per_customer_limit,
        "used_count": int(c.used_count or 0), "enabled": bool(c.enabled),
        "archived": c.archived_at is not None, "archived_at": _iso(c.archived_at),
        "starts_at": _iso(c.starts_at), "expires_at": _iso(c.expires_at),
        "created_at": _iso(c.created_at),
    }


def _norm_credit_code(raw: str) -> str:
    code = storefront_credit.normalize_code(raw)
    if not 1 <= len(code) <= 32:
        raise AdminCommandError("validation", "code must be 1..32 characters")
    return code


def _validate_credit_economics(
    *, kind, percent_off, amount_toman, max_bonus_toman, min_topup_toman, is_gift,
    max_uses, per_customer_limit, starts_at, expires_at,  # noqa: ANN001
) -> dict:
    """Validate + normalize the economic/scheduling fields (shared by create + edit). Returns a dict
    with the canonical values (e.g. a percent code has amount_toman cleared, a gift has min_topup 0)."""
    if kind not in _CREDIT_KINDS:
        raise AdminCommandError("validation", "kind must be percent or fixed")
    is_gift = bool(is_gift)
    if is_gift and kind != "fixed":
        raise AdminCommandError("validation", "a gift code must be a fixed amount")
    percent_off = int(percent_off) if percent_off is not None else None
    amount_toman = int(amount_toman) if amount_toman is not None else None
    max_bonus_toman = int(max_bonus_toman) if max_bonus_toman is not None else None
    if kind == "percent":
        if percent_off is None or not 1 <= percent_off <= 100:
            raise AdminCommandError("validation", "percent_off must be 1..100 for a percent code")
        amount_toman = None
        if max_bonus_toman is not None and not 1 <= max_bonus_toman <= 10**12:
            raise AdminCommandError("validation", "max_bonus_toman out of range")
    else:  # fixed / gift
        if amount_toman is None or not 1 <= amount_toman <= 10**12:
            raise AdminCommandError("validation", "amount_toman must be 1..10^12 for a fixed code")
        percent_off = None
        max_bonus_toman = None
    min_topup_toman = int(min_topup_toman or 0)
    if not 0 <= min_topup_toman <= 10**12:
        raise AdminCommandError("validation", "min_topup_toman out of range")
    if is_gift:
        min_topup_toman = 0  # a gift is standalone; it ignores the top-up amount
    if max_uses is not None and not 1 <= int(max_uses) <= 10**9:
        raise AdminCommandError("validation", "max_uses must be 1..10^9")
    if per_customer_limit is not None and not 1 <= int(per_customer_limit) <= 10**9:
        raise AdminCommandError("validation", "per_customer_limit must be 1..10^9")
    s_at = _as_utc(starts_at) if isinstance(starts_at, dt.datetime) else None
    e_at = _as_utc(expires_at) if isinstance(expires_at, dt.datetime) else None
    if s_at and e_at and s_at >= e_at:
        raise AdminCommandError("validation", "expires_at must be after starts_at")
    return {
        "kind": kind, "percent_off": percent_off, "amount_toman": amount_toman,
        "max_bonus_toman": max_bonus_toman, "min_topup_toman": min_topup_toman, "is_gift": is_gift,
        "max_uses": int(max_uses) if max_uses is not None else None,
        "per_customer_limit": int(per_customer_limit) if per_customer_limit is not None else None,
        "starts_at": s_at, "expires_at": e_at,
    }


async def _owned_credit_code(
    session: AsyncSession, shop_id: int, code_id: int
) -> StorefrontCreditCode:
    code = await session.get(StorefrontCreditCode, code_id)
    if code is None or code.storefront_bot_id != shop_id:
        raise AdminCommandError("not_found", "Credit code not found")
    return code


async def create_credit(
    session: AsyncSession, shop_id: int, ctx: CommandContext, *, code: str, kind: str,
    percent_off: int | None = None, amount_toman: int | None = None,
    max_bonus_toman: int | None = None, min_topup_toman: int = 0, is_gift: bool = False,
    max_uses: int | None = None, per_customer_limit: int | None = 1,
    starts_at: dt.datetime | None = None, expires_at: dt.datetime | None = None,
) -> CommandResult:
    """Create a credit code (201). Uniqueness is enforced per shop on the normalized code."""
    code_ci = _norm_credit_code(code)
    fields = _validate_credit_economics(
        kind=kind, percent_off=percent_off, amount_toman=amount_toman,
        max_bonus_toman=max_bonus_toman, min_topup_toman=min_topup_toman, is_gift=is_gift,
        max_uses=max_uses, per_customer_limit=per_customer_limit,
        starts_at=starts_at, expires_at=expires_at)
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    intent = {"code_ci": code_ci, **_jsonable(fields)}
    command, replay = await _claim_entity_command(
        session, shop, ctx, action="credit.create", intent=intent)
    if replay is not None:
        return replay
    if await storefront_credit.code_exists(session, shop.id, code_ci):
        exc = AdminCommandError(
            "code_exists", "code already exists", response_status=422,
            response_body={"error": "code_exists"})
        await _cache_known_failure(session, shop, ctx, action="credit.create", command=command, exc=exc)
        raise exc

    async def mutate() -> _Mutation:
        c = storefront_credit.create_code_core(session, shop.id, code=code, **fields)
        await session.flush()
        d = _credit_dict(c)
        return _Mutation({"credit": d}, d, "credit_code", c.id, response_status=201)

    return await _execute_entity(
        session, shop, ctx, action="credit.create", command=command, before=None, mutate=mutate)


async def update_credit(
    session: AsyncSession, shop_id: int, code_id: int, ctx: CommandContext, *, changes: dict,
) -> CommandResult:
    """Partially edit a code. Before the first redemption every economic/scheduling field is editable;
    after ANY redemption only `enabled` + `expires_at` may change (money already moved). `code` is
    always immutable."""
    unknown = set(changes) - _CREDIT_EDITABLE
    if unknown:
        raise AdminCommandError("validation", f"unknown fields: {sorted(unknown)}")
    if not changes:
        raise AdminCommandError("validation", "no fields to update")
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    intent = {"code_id": int(code_id), "changes": _jsonable(changes)}
    command, replay = await _claim_entity_command(
        session, shop, ctx, action="credit.edit", intent=intent)
    if replay is not None:
        return replay
    try:
        code = await _owned_credit_code(session, shop_id, code_id)
    except AdminCommandError as exc:
        await _cache_known_failure(session, shop, ctx, action="credit.edit", command=command, exc=exc)
        raise
    redeemed = int(code.used_count or 0) > 0 or await storefront_credit.has_redemptions(session, code.id)
    if redeemed:
        locked = set(changes) - _CREDIT_POST_REDEMPTION
        if locked:
            err = AdminCommandError(
                "locked_after_redemption", "locked_after_redemption", response_status=422,
                response_body={"error": "locked_after_redemption", "fields": sorted(locked)})
            await _cache_known_failure(
                session, shop, ctx, action="credit.edit", command=command, exc=err)
            raise err
    effective = {
        "kind": changes.get("kind", code.kind),
        "percent_off": changes.get("percent_off", code.percent_off),
        "amount_toman": changes.get("amount_toman", code.amount_toman),
        "max_bonus_toman": changes.get("max_bonus_toman", code.max_bonus_toman),
        "min_topup_toman": changes.get("min_topup_toman", code.min_topup_toman),
        "is_gift": changes.get("is_gift", code.is_gift),
        "max_uses": changes.get("max_uses", code.max_uses),
        "per_customer_limit": changes.get("per_customer_limit", code.per_customer_limit),
        "starts_at": changes.get("starts_at", code.starts_at),
        "expires_at": changes.get("expires_at", code.expires_at),
    }
    try:
        validated = _validate_credit_economics(**effective)
    except AdminCommandError as exc:
        await _cache_known_failure(session, shop, ctx, action="credit.edit", command=command, exc=exc)
        raise
    apply_fields = {
        k: (bool(changes[k]) if k == "enabled" else validated[k]) for k in changes
    }
    before = _credit_dict(code)

    async def mutate() -> _Mutation:
        storefront_credit.update_code_core(code, apply_fields)
        await session.flush()
        d = _credit_dict(code)
        return _Mutation({"credit": d}, d, "credit_code", code.id)

    return await _execute_entity(
        session, shop, ctx, action="credit.edit", command=command, before=before, mutate=mutate)


async def set_credit_enabled(
    session: AsyncSession, shop_id: int, code_id: int, ctx: CommandContext, *, enabled: bool,
) -> CommandResult:
    """Absolute enable/disable of a code (does NOT archive)."""
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    intent = {"code_id": int(code_id), "enabled": bool(enabled)}
    command, replay = await _claim_entity_command(
        session, shop, ctx, action="credit.enable", intent=intent)
    if replay is not None:
        return replay
    try:
        code = await _owned_credit_code(session, shop_id, code_id)
    except AdminCommandError as exc:
        await _cache_known_failure(
            session, shop, ctx, action="credit.enable", command=command, exc=exc)
        raise
    if enabled and code.archived_at is not None:
        # Reviving an archived code would make it redeemable again while staying hidden from the
        # normal list — a live code the shop can't see to switch off.
        err = AdminCommandError(
            "archived_code", "این کد بایگانی شده است و دیگر قابلِ فعال‌سازی نیست؛ یک کدِ جدید بسازید.",
            response_status=422)
        await _cache_known_failure(
            session, shop, ctx, action="credit.enable", command=command, exc=err)
        raise err
    before = {"enabled": bool(code.enabled)}

    async def mutate() -> _Mutation:
        storefront_credit.set_enabled_core(code, enabled)
        await session.flush()
        d = _credit_dict(code)
        return _Mutation({"credit": d}, d, "credit_code", code.id)

    return await _execute_entity(
        session, shop, ctx, action="credit.enable", command=command, before=before, mutate=mutate)


async def archive_credit(
    session: AsyncSession, shop_id: int, code_id: int, ctx: CommandContext,
) -> CommandResult:
    """Archive a code (disable + stamp `archived_at`), preserving its redemption history and unique
    code. Idempotent (re-archiving replays the same result)."""
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    intent = {"code_id": int(code_id)}
    command, replay = await _claim_entity_command(
        session, shop, ctx, action="credit.archive", intent=intent)
    if replay is not None:
        return replay
    try:
        code = await _owned_credit_code(session, shop_id, code_id)
    except AdminCommandError as exc:
        await _cache_known_failure(
            session, shop, ctx, action="credit.archive", command=command, exc=exc)
        raise
    before = _credit_dict(code)

    async def mutate() -> _Mutation:
        storefront_credit.archive_code_core(code)
        await session.flush()
        d = _credit_dict(code)
        return _Mutation({"credit": d}, d, "credit_code", code.id)

    return await _execute_entity(
        session, shop, ctx, action="credit.archive", command=command, before=before, mutate=mutate)


# ── credit reads (tenant-gated; not commands) ─────────────────────────────────

async def list_credits(
    session: AsyncSession, shop_id: int, actor_id: int, source: Source, *,
    include_archived: bool = False, cursor: str | None = None, limit: int = 50,
) -> dict:
    shop, _ = await _authorized_shop(session, shop_id, actor_id, source)
    codes, nxt = await storefront_credit.list_credit_codes(
        session, shop.id, include_archived=include_archived, cursor=cursor, limit=limit)
    return {"config_version": shop.config_version,
            "items": [_credit_dict(c) for c in codes], "next_cursor": nxt}


async def credit_usage(
    session: AsyncSession, shop_id: int, code_id: int, actor_id: int, source: Source,
) -> dict:
    shop, _ = await _authorized_shop(session, shop_id, actor_id, source)
    code = await _owned_credit_code(session, shop.id, code_id)
    total, unique, bonus = await storefront_credit.usage_summary(session, code.id)
    return {"config_version": shop.config_version, "code": _credit_dict(code),
            "total_redemptions": total, "unique_customers": unique, "total_bonus_toman": bonus}


async def list_credit_redemptions(
    session: AsyncSession, shop_id: int, code_id: int, actor_id: int, source: Source, *,
    cursor: str | None = None, limit: int = 50,
) -> dict:
    shop, _ = await _authorized_shop(session, shop_id, actor_id, source)
    code = await _owned_credit_code(session, shop.id, code_id)
    rows, nxt = await storefront_credit.list_redemptions(
        session, code.id, cursor=cursor, limit=limit)
    items = [{"id": r.id, "customer_id": r.customer_id, "wallet_txn_id": r.wallet_txn_id,
              "bonus_toman": int(r.bonus_toman or 0), "created_at": _iso(r.created_at)} for r in rows]
    return {"config_version": shop.config_version, "items": items, "next_cursor": nxt}


# ── communications: durable broadcasts + direct messages ──────────────────────
# A broadcast/direct is enqueued as a job + a create-time recipient snapshot (storefront_delivery),
# then delivered by the durable worker. Enqueue is a 202 entity command; the request path never
# touches Telegram.

_DIRECT_RATE_LIMIT = 10       # owner-typed direct messages per shop per minute
_DIRECT_RATE_WINDOW = 60


def _job_dict(j: StorefrontBroadcastJob) -> dict:
    return {
        "id": j.id, "kind": j.kind, "segment": j.segment, "status": j.status,
        "text": j.message_text, "total": int(j.total_count or 0), "sent": int(j.sent_count or 0),
        "blocked": int(j.blocked_count or 0), "failed": int(j.failed_count or 0),
        "pending": int(j.pending_count or 0), "created_at": _iso(j.created_at),
        "canceled_at": _iso(j.canceled_at),
    }


async def _owned_broadcast_job(
    session: AsyncSession, shop_id: int, job_id: int
) -> StorefrontBroadcastJob:
    job = await session.get(StorefrontBroadcastJob, job_id)
    if job is None or job.storefront_bot_id != shop_id:
        raise AdminCommandError("not_found", "Broadcast not found")
    return job


async def _enforce_direct_rate(session: AsyncSession, shop_id: int, actor_id: int) -> None:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=_DIRECT_RATE_WINDOW)
    n = await session.scalar(
        select(func.count()).select_from(StorefrontBroadcastJob).where(
            StorefrontBroadcastJob.storefront_bot_id == shop_id,
            StorefrontBroadcastJob.kind == "direct",
            StorefrontBroadcastJob.actor_telegram_id == actor_id,
            StorefrontBroadcastJob.created_at >= cutoff,
        ))
    if int(n or 0) >= _DIRECT_RATE_LIMIT:
        raise AdminCommandError(
            "rate_limited", "rate_limited", response_status=422,
            response_body={"error": "rate_limited"})


async def enqueue_broadcast(
    session: AsyncSession, shop_id: int, ctx: CommandContext, *, segment: str, text: str,
) -> CommandResult:
    """Queue a segment broadcast (202). Recipients are snapshotted at create time; delivery is the
    worker's job. A same key + same text replays the same job (no second fan-out); a same key +
    different text is an idempotency conflict."""
    from app.services import storefront_delivery
    text = (text or "").strip()
    if not 1 <= len(text) <= 4000:
        raise AdminCommandError("validation", "text must be 1..4000 characters")
    if segment not in storefront.SEGMENTS:
        raise AdminCommandError("validation", "unknown segment")
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    intent = {"kind": "broadcast", "segment": segment,
              "text_hash": hashlib.sha256(text.encode()).hexdigest()}
    command, replay = await _claim_entity_command(
        session, shop, ctx, action="broadcast.create", intent=intent)
    if replay is not None:
        return replay
    customers = await storefront.customers_in_segment(session, shop.id, segment)
    if len(customers) > storefront.AUDIENCE_CAP:
        exc = AdminCommandError(
            "audience_too_large", f"audience exceeds {storefront.AUDIENCE_CAP}", response_status=422,
            response_body={"error": "audience_too_large", "count": len(customers)})
        await _cache_known_failure(
            session, shop, ctx, action="broadcast.create", command=command, exc=exc)
        raise exc

    async def mutate() -> _Mutation:
        job = await storefront_delivery.snapshot_job(
            session, storefront_bot_id=shop.id, kind="broadcast", segment=segment,
            message_text=text, actor_telegram_id=ctx.actor_telegram_id,
            idempotency_key=ctx.idempotency_key, customers=customers)
        await session.flush()
        body = {"job_id": job.id, "status": job.status, "total": int(job.total_count or 0)}
        return _Mutation(body, {"job_id": job.id, "segment": segment, "total": job.total_count},
                         "broadcast_job", job.id, response_status=202)

    return await _execute_entity(
        session, shop, ctx, action="broadcast.create", command=command, before=None, mutate=mutate)


async def enqueue_direct(
    session: AsyncSession, shop_id: int, customer_id: int, ctx: CommandContext, *, text: str,
) -> CommandResult:
    """Queue a durable direct message to one customer (202). Owner/co-admin-typed messages are rate
    limited (10/shop/min); `source='system'` (e.g. a top-up decision notice) bypasses that gate."""
    from app.services import storefront_delivery
    text = (text or "").strip()
    if not 1 <= len(text) <= 4000:
        raise AdminCommandError("validation", "text must be 1..4000 characters")
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    intent = {"kind": "direct", "customer_id": int(customer_id),
              "text_hash": hashlib.sha256(text.encode()).hexdigest()}
    command, replay = await _claim_entity_command(
        session, shop, ctx, action="message.direct", intent=intent)
    if replay is not None:
        return replay
    try:
        if ctx.source != "system":
            await _enforce_direct_rate(session, shop.id, ctx.actor_telegram_id)
        cust = await _owned_customer(session, shop_id, customer_id)
        if cust.banned:
            raise AdminCommandError(
                "banned", "customer is banned", response_status=422,
                response_body={"error": "banned"})
    except AdminCommandError as exc:
        await _cache_known_failure(
            session, shop, ctx, action="message.direct", command=command, exc=exc)
        raise

    async def mutate() -> _Mutation:
        job = await storefront_delivery.snapshot_job(
            session, storefront_bot_id=shop.id, kind="direct", segment=None, message_text=text,
            actor_telegram_id=ctx.actor_telegram_id, idempotency_key=ctx.idempotency_key,
            customers=[cust])
        await session.flush()
        body = {"delivery_id": job.id, "status": job.status, "total": int(job.total_count or 0)}
        return _Mutation(body, {"delivery_id": job.id, "customer_id": cust.id},
                         "broadcast_job", job.id, response_status=202)

    return await _execute_entity(
        session, shop, ctx, action="message.direct", command=command, before=None, mutate=mutate)


async def cancel_broadcast(
    session: AsyncSession, shop_id: int, job_id: int, ctx: CommandContext,
) -> CommandResult:
    """Cancel a job's still-unsent recipients (already-claimed 'sending' rows may finish)."""
    from app.services import storefront_delivery
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    intent = {"job_id": int(job_id)}
    command, replay = await _claim_entity_command(
        session, shop, ctx, action="broadcast.cancel", intent=intent)
    if replay is not None:
        return replay
    try:
        job = await _owned_broadcast_job(session, shop_id, job_id)
    except AdminCommandError as exc:
        await _cache_known_failure(
            session, shop, ctx, action="broadcast.cancel", command=command, exc=exc)
        raise
    before = {"status": job.status}

    async def mutate() -> _Mutation:
        await storefront_delivery.cancel_unsent(session, job.id)
        await session.flush()
        j = await session.get(StorefrontBroadcastJob, job.id)
        assert j is not None
        return _Mutation(_job_dict(j), {"status": j.status}, "broadcast_job", job.id)

    return await _execute_entity(
        session, shop, ctx, action="broadcast.cancel", command=command, before=before, mutate=mutate)


# ── communications reads (tenant-gated; not commands) ─────────────────────────

async def preview_audience(
    session: AsyncSession, shop_id: int, segment: str, actor_id: int, source: Source,
) -> dict:
    shop, _ = await _authorized_shop(session, shop_id, actor_id, source)
    try:
        data = await storefront.audience_preview(session, shop.id, segment)
    except ValueError as exc:
        raise AdminCommandError("validation", str(exc), response_status=422) from exc
    return {"config_version": shop.config_version, **data}


async def get_broadcast(
    session: AsyncSession, shop_id: int, job_id: int, actor_id: int, source: Source,
) -> dict:
    shop, _ = await _authorized_shop(session, shop_id, actor_id, source)
    job = await _owned_broadcast_job(session, shop.id, job_id)
    return {"config_version": shop.config_version, "job": _job_dict(job)}


async def list_broadcasts(
    session: AsyncSession, shop_id: int, actor_id: int, source: Source, *,
    cursor: str | None = None, limit: int = 50,
) -> dict:
    from app.services import storefront_cursor
    shop, _ = await _authorized_shop(session, shop_id, actor_id, source)
    endpoint = f"broadcasts:{shop.id}"
    where = [StorefrontBroadcastJob.storefront_bot_id == shop.id]
    if cursor:
        c_at, c_id = storefront_cursor.decode_cursor(endpoint, cursor)
        where.append(
            (StorefrontBroadcastJob.created_at < c_at)
            | ((StorefrontBroadcastJob.created_at == c_at) & (StorefrontBroadcastJob.id < c_id)))
    rows = list((await session.execute(
        select(StorefrontBroadcastJob).where(*where)
        .order_by(StorefrontBroadcastJob.created_at.desc(), StorefrontBroadcastJob.id.desc())
        .limit(limit + 1))).scalars().all())
    page = rows[:limit]
    nxt = (storefront_cursor.encode_cursor(
        endpoint=endpoint, created_at=page[-1].created_at, row_id=page[-1].id)
        if len(rows) > limit and page else None)
    return {"config_version": shop.config_version,
            "items": [_job_dict(j) for j in page], "next_cursor": nxt}
