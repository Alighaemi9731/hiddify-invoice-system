"""Storefront audit, canonical hashing and leased idempotency primitives.

These helpers never commit.  A DB-only command, its configuration mutation, cached response and
success audit therefore commit (or roll back) as one unit in ``storefront_admin``.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StorefrontApiCommand, StorefrontAuditEvent

LEASE_SECONDS = 60
MAX_RESPONSE_BYTES = 16 * 1024
RETENTION_DAYS = 30

_SENSITIVE_PARTS = (
    "token", "secret", "password", "credential", "api_key", "proxy_path",
    "proof", "sub_link", "subscription_link", "card_number", "card_holder", "usdt_address",
    "ton_address", "channel_link",
)


def _json_default(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Stable UTF-8 JSON used for request identity and response size enforcement."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_json_default,
        allow_nan=False,
    )


def canonical_request_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _sensitive(key: str) -> bool:
    normalized = key.casefold()
    return any(part in normalized for part in _SENSITIVE_PARTS)


def redact(value: Any, *, key: str = "") -> Any:
    """Recursively remove known credential/proof/subscription fields from persisted JSON."""
    if key and _sensitive(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, (dt.date, dt.datetime, Decimal, Enum)):
        return _json_default(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return "[REDACTED]"


def safe_cached_response(value: dict | list | None) -> dict | list | None:
    """Return explicit redacted JSON guaranteed not to exceed 16 KiB."""
    safe = redact(value)
    encoded = canonical_json(safe).encode("utf-8")
    if len(encoded) <= MAX_RESPONSE_BYTES:
        return safe
    return {
        "truncated": True,
        "original_size_bytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


async def append_event(
    session: AsyncSession,
    *,
    storefront_bot_id: int | None,
    actor_telegram_id: int | None,
    actor_role: str,
    source: Literal["bot", "portal", "system"],
    action: str,
    outcome: str,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    correlation_id: str | None = None,
    before: dict | list | None = None,
    after: dict | list | None = None,
    error_class: str | None = None,
) -> StorefrontAuditEvent:
    event = StorefrontAuditEvent(
        storefront_bot_id=storefront_bot_id,
        actor_telegram_id=actor_telegram_id,
        actor_role=actor_role[:16],
        source=source,
        action=action[:64],
        entity_type=entity_type[:32] if entity_type else None,
        entity_id=str(entity_id)[:64] if entity_id is not None else None,
        correlation_id=correlation_id[:128] if correlation_id else None,
        before_json=redact(before),
        after_json=redact(after),
        outcome=outcome[:16],
        error_class=error_class[:32] if error_class else None,
    )
    session.add(event)
    await session.flush()
    return event


ClaimOutcome = Literal["claimed", "replay", "conflict", "in_flight", "unknown"]


@dataclass(frozen=True)
class CommandClaim:
    outcome: ClaimOutcome
    command: StorefrontApiCommand


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value.astimezone(
        dt.timezone.utc
    )


async def _existing_command(
    session: AsyncSession, shop_id: int, actor_id: int, key: str
) -> StorefrontApiCommand | None:
    stmt = select(StorefrontApiCommand).where(
        StorefrontApiCommand.storefront_bot_id == shop_id,
        StorefrontApiCommand.actor_telegram_id == actor_id,
        StorefrontApiCommand.idempotency_key == key,
    )
    try:
        stmt = stmt.with_for_update()
    except Exception:  # pragma: no cover - SQLAlchemy dialect fallback
        pass
    return (await session.execute(stmt)).scalar_one_or_none()


async def claim_command(
    session: AsyncSession,
    *,
    shop_id: int,
    actor_telegram_id: int,
    idempotency_key: str,
    action: str,
    request: Any,
    external_io: bool = False,
    now: dt.datetime | None = None,
) -> CommandClaim:
    """Claim, replay or safely recover a command under its per-actor idempotency key."""
    key = idempotency_key.strip()
    if not key or len(key) > 128:
        raise ValueError("idempotency_key must contain 1..128 characters")
    if not action or len(action) > 64:
        raise ValueError("action must contain 1..64 characters")
    request_hash = canonical_request_hash(request)
    current = now or dt.datetime.now(dt.timezone.utc)
    lease = current + dt.timedelta(seconds=LEASE_SECONDS)

    existing = await _existing_command(session, shop_id, actor_telegram_id, key)
    if existing is None:
        values = {
            "storefront_bot_id": shop_id, "actor_telegram_id": actor_telegram_id,
            "idempotency_key": key, "action": action, "request_hash": request_hash,
            "status": "pending", "external_io": external_io, "lease_expires_at": lease,
            "attempt_count": 1,
        }
        dialect = session.get_bind().dialect.name
        if dialect in ("sqlite", "postgresql"):
            insert_factory = sqlite_insert if dialect == "sqlite" else pg_insert
            stmt = insert_factory(StorefrontApiCommand).values(**values).on_conflict_do_nothing(
                index_elements=["storefront_bot_id", "actor_telegram_id", "idempotency_key"]
            )
            result = await session.execute(stmt)
            existing = await _existing_command(session, shop_id, actor_telegram_id, key)
            if existing is None:
                raise RuntimeError("idempotency insert completed without a durable command")
            if int(result.rowcount or 0) == 1:  # type: ignore[attr-defined]
                return CommandClaim("claimed", existing)
        else:  # pragma: no cover - supported production/test dialects use conflict-safe INSERT
            command = StorefrontApiCommand(**values)
            try:
                async with session.begin_nested():
                    session.add(command)
                    await session.flush()
                return CommandClaim("claimed", command)
            except IntegrityError:
                existing = await _existing_command(session, shop_id, actor_telegram_id, key)
                if existing is None:
                    raise

    if existing.action != action or existing.request_hash != request_hash:
        return CommandClaim("conflict", existing)
    if existing.status in ("succeeded", "failed"):
        return CommandClaim("replay", existing)
    if existing.status == "unknown":
        return CommandClaim("unknown", existing)
    if existing.lease_expires_at is not None and _utc(existing.lease_expires_at) > _utc(current):
        return CommandClaim("in_flight", existing)
    if existing.external_io or external_io:
        existing.status = "unknown"
        existing.lease_expires_at = None
        existing.error_class = "lease_expired_external"
        await session.flush()
        return CommandClaim("unknown", existing)
    existing.lease_expires_at = lease
    existing.attempt_count += 1
    existing.response_status = None
    existing.response_body = None
    existing.error_class = None
    await session.flush()
    return CommandClaim("claimed", existing)


async def finalize_command(
    session: AsyncSession,
    command: StorefrontApiCommand,
    *,
    succeeded: bool,
    response_status: int,
    response_body: dict | list | None,
    error_class: str | None = None,
) -> None:
    if command.status != "pending":
        raise ValueError("only pending commands can be finalized")
    command.status = "succeeded" if succeeded else "failed"
    command.response_status = int(response_status)
    command.response_body = safe_cached_response(response_body)
    command.error_class = error_class[:32] if error_class else None
    command.lease_expires_at = None
    await session.flush()


async def prune_commands(
    session: AsyncSession, *, now: dt.datetime | None = None
) -> int:
    current = now or dt.datetime.now(dt.timezone.utc)
    cutoff = current - dt.timedelta(days=RETENTION_DAYS)
    await session.execute(
        update(StorefrontApiCommand)
        .where(
            StorefrontApiCommand.status == "pending",
            StorefrontApiCommand.external_io.is_(True),
            StorefrontApiCommand.lease_expires_at.is_not(None),
            StorefrontApiCommand.lease_expires_at < current,
        )
        .values(
            status="unknown", lease_expires_at=None,
            error_class="lease_expired_external",
        )
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(
        delete(StorefrontApiCommand).where(
            StorefrontApiCommand.created_at < cutoff,
            StorefrontApiCommand.status.in_(("succeeded", "failed", "unknown")),
        )
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
