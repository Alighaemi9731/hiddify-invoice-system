"""Shared absolute-state administration commands for storefront bot and portal.

All DB-only commands use one transaction for: idempotency claim, atomic config-version CAS, domain
mutation, redacted audit and cached response.  Functions authorize the actor and child ownership in
the service layer; callers cannot make a foreign id safe by routing alone.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import SessionLocal
from app.models import (
    Reseller,
    StorefrontApiCommand,
    StorefrontAuditEvent,
    StorefrontBot,
    StorefrontCustomer,
    StorefrontOrder,
    StorefrontPlan,
)
from app.services import storefront, storefront_audit

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
        "not_found": 404, "validation": 422, "config_conflict": 409,
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
        "id": plan.id, "title": plan.title, "gb": plan.gb, "days": plan.days,
        "price_toman": plan.price_toman, "enabled": plan.enabled, "sort_order": plan.sort_order,
    }


def _validate_plan(*, title: str, gb: int, days: int, price_toman: int) -> tuple[str, int, int, int]:
    title = title.strip()
    if len(title) > 128:
        raise AdminCommandError("validation", "title exceeds 128 characters")
    if not 1 <= int(gb) <= 100_000:
        raise AdminCommandError("validation", "gb must be between 1 and 100000")
    if not 1 <= int(days) <= 3650:
        raise AdminCommandError("validation", "days must be between 1 and 3650")
    if not 0 <= int(price_toman) <= 10**12:
        raise AdminCommandError("validation", "price_toman is out of range")
    return title, int(gb), int(days), int(price_toman)


async def list_plans(
    session: AsyncSession, shop_id: int, actor_id: int, source: Source
) -> dict:
    shop, _ = await _authorized_shop(session, shop_id, actor_id, source)
    plans = await storefront.list_plans(session, shop.id)
    return {"config_version": shop.config_version, "plans": [_plan_dict(plan) for plan in plans]}


async def create_plan(
    session: AsyncSession, shop_id: int, ctx: CommandContext, *, title: str = "",
    gb: int, days: int, price_toman: int,
) -> CommandResult:
    title, gb, days, price_toman = _validate_plan(
        title=title, gb=gb, days=days, price_toman=price_toman)
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    request = {"title": title, "gb": gb, "days": days, "price_toman": price_toman}
    command, replay = await _claim_db_command(
        session, shop, ctx, action="plan.create", intent=request)
    if replay is not None:
        return replay
    plan_count = len(await storefront.list_plans(session, shop.id))
    if plan_count >= 500:
        exc = AdminCommandError("validation", "a storefront can have at most 500 plans")
        await _cache_known_failure(
            session, shop, ctx, action="plan.create", command=command, exc=exc)
        raise exc

    async def mutate(_version: int) -> _Mutation:
        plan = StorefrontPlan(
            storefront_bot_id=shop.id, title=title, gb=gb, days=days,
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
    title: str | None = None, gb: int | None = None, days: int | None = None,
    price_toman: int | None = None,
) -> CommandResult:
    intent: dict[str, Any] = {"plan_id": plan_id}
    if title is not None:
        normalized_title = title.strip()
        if len(normalized_title) > 128:
            raise AdminCommandError("validation", "title exceeds 128 characters")
        intent["title"] = normalized_title
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
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    command, replay = await _claim_db_command(
        session, shop, ctx, action="plan.update", intent=intent)
    if replay is not None:
        return replay
    try:
        plan = await _owned_plan(session, shop.id, plan_id)
        before = _plan_dict(plan)
        values = _validate_plan(
            title=str(intent.get("title", plan.title)), gb=int(intent.get("gb", plan.gb)),
            days=int(intent.get("days", plan.days)),
            price_toman=int(intent.get("price_toman", plan.price_toman)),
        )
    except AdminCommandError as exc:
        await _cache_known_failure(
            session, shop, ctx, action="plan.update", command=command, exc=exc)
        raise

    async def mutate(_version: int) -> _Mutation:
        plan.title, plan.gb, plan.days, plan.price_toman = values
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
    shop, _ = await _authorized_shop(session, shop_id, ctx.actor_telegram_id, ctx.source)
    intent = {"plan_id": plan_id, "enabled": bool(enabled)}
    command, replay = await _claim_db_command(
        session, shop, ctx, action="plan.set_enabled", intent=intent)
    if replay is not None:
        return replay
    try:
        plan = await _owned_plan(session, shop.id, plan_id)
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
        if not 1 <= intent["gb"] <= 1000:
            raise AdminCommandError("validation", "trial gb out of range")
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
        "welcome_text": shop.welcome_text or "🛍 به فروشگاه خوش آمدید!",
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
    # Definite validation failure (order not in an actionable state).
    if result.reason in ("not_found", "trial"):
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
    outcome, code, status = ("failed", "validation", 422) if reason in ("not_found", "trial") else (
        ("failed", "in_flight", 409) if reason == "processing" else ("failed", "external_failure", 502))
    await storefront_audit.append_event(
        session, storefront_bot_id=shop.id, actor_telegram_id=ctx.actor_telegram_id,
        actor_role=role, source=ctx.source, action="order.renew", outcome=outcome,
        entity_type="order", entity_id=order_id,
        correlation_id=ctx.correlation_id or ctx.idempotency_key, error_class=reason)
    await session.commit()
    raise AdminCommandError(
        code, result.message or reason, response_status=status,
        response_body={"error": reason, "order_id": order_id})
