"""Tenant-safe storefront reporting and administration for the reseller web portal."""
from __future__ import annotations

import datetime as dt
import os
import re
import uuid
from typing import Any, Literal, NoReturn

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.session import new_session
from app.core.db import get_session
from app.core.portal_auth import ResellerContext, get_current_reseller
from app.core.storefront_access import (
    StorefrontAccess,
    get_storefront_access,
    list_owned_storefronts,
)
from app.models import StorefrontWalletTxn
from app.schemas.portal_storefront import (
    StorefrontBroadcastBody,
    StorefrontBulkDecisionBody,
    StorefrontChannelBody,
    StorefrontChannelOut,
    StorefrontCreditCreate,
    StorefrontCreditUpdate,
    StorefrontCustomerPreviewOut,
    StorefrontCustomerStatusBody,
    StorefrontDashboardOut,
    StorefrontDirectMessageBody,
    StorefrontEnabledBody,
    StorefrontFinanceOut,
    StorefrontHealthOut,
    StorefrontManagerBody,
    StorefrontManagerOut,
    StorefrontManagersOut,
    StorefrontMessageSettingsBody,
    StorefrontMessageSettingsOut,
    StorefrontMutationOut,
    StorefrontOrderDeleteBody,
    StorefrontOrderEnabledBody,
    StorefrontPaymentSettingsBody,
    StorefrontPaymentSettingsOut,
    StorefrontPlanCreate,
    StorefrontPlanHistoryItemOut,
    StorefrontPlanHistoryOut,
    StorefrontPlanOrderBody,
    StorefrontPlanOut,
    StorefrontPlansOut,
    StorefrontPlanUpdate,
    StorefrontPreviewFreeTrialOut,
    StorefrontPreviewPlanOut,
    StorefrontSettingsOut,
    StorefrontShopStateBody,
    StorefrontShopStateOut,
    StorefrontSummaryOut,
    StorefrontTopupDecisionBody,
    StorefrontTrialSettingsBody,
    StorefrontTrialSettingsOut,
    StorefrontWalletAdjustmentBody,
)
from app.services import (
    pricing,
    settings_service,
    storefront,
    storefront_admin,
    storefront_cursor,
    storefront_customers,
    storefront_pricing,
    storefront_provision,
    storefront_reporting,
)

router = APIRouter(prefix="/api/portal/storefronts", tags=["portal-storefront"])
_ETAG = re.compile(r'^"sf-config-([1-9][0-9]*)"$')


def _etag(version: int) -> str:
    return f'"sf-config-{version}"'


def _set_etag(response: Response, version: int) -> None:
    response.headers["ETag"] = _etag(version)
    response.headers["Cache-Control"] = "private, no-store"


def _expected_version(if_match: str) -> int:
    match = _ETAG.fullmatch(if_match.strip())
    if match is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_if_match", "message": "If-Match must be a storefront ETag"},
        )
    return int(match.group(1))


def _command_context(
    ctx: ResellerContext,
    *,
    if_match: str,
    idempotency_key: str,
) -> storefront_admin.CommandContext:
    key = idempotency_key.strip()
    if not key or len(key) > 128:
        raise HTTPException(status_code=422, detail={"code": "invalid_idempotency_key"})
    return storefront_admin.CommandContext(
        actor_telegram_id=ctx.chat_id,
        actor_role="owner",
        source="portal",
        idempotency_key=key,
        expected_version=_expected_version(if_match),
        correlation_id=str(uuid.uuid4()),
    )


def _raise_admin_error(exc: storefront_admin.AdminCommandError) -> NoReturn:
    if exc.code == "not_found":
        status_code = 404
    elif exc.code in {"config_conflict", "idempotency_conflict", "in_flight", "unknown"}:
        status_code = 409
    elif exc.code in {"external_failure", "external_unknown"}:
        status_code = 502
    else:
        status_code = 422
    detail: dict[str, Any] = {"code": exc.code, "message": str(exc)}
    if exc.current_version is not None:
        detail["current_version"] = exc.current_version
        detail["current_etag"] = _etag(exc.current_version)
    raise HTTPException(status_code=status_code, detail=detail) from exc


def _check_result(result: storefront_admin.CommandResult) -> None:
    if result.response_status < 400:
        return
    body = result.body if isinstance(result.body, dict) else {}
    code = str(body.get("error") or "command_failed")
    raise HTTPException(
        status_code=result.response_status,
        detail={
            "code": code,
            "current_version": result.config_version,
            "current_etag": _etag(result.config_version),
        },
    )


def _result_dict(result: storefront_admin.CommandResult) -> dict[str, Any]:
    return result.body if isinstance(result.body, dict) else {}


def _mutation_dict(
    response: Response, result: storefront_admin.CommandResult,
) -> StorefrontMutationOut[dict[str, Any]]:
    _check_result(result)
    _set_etag(response, result.config_version)
    return StorefrontMutationOut(result=_result_dict(result), config_version=result.config_version)


async def _verify_channel_with_telegram(
    token: str, channel_id: str,
) -> tuple[bool, str | None]:
    verified = False
    resolved_link: str | None = None
    tg_bot: Bot | None = None
    try:
        tg_bot = Bot(token=token, session=new_session())
        me = await tg_bot.get_me()
        member = await tg_bot.get_chat_member(channel_id, me.id)
        status_value = getattr(member.status, "value", str(member.status))
        verified = status_value in {"administrator", "creator"}
        if not verified:
            return False, None
        try:
            chat = await tg_bot.get_chat(channel_id)
            username = getattr(chat, "username", None)
            if username:
                resolved_link = f"https://t.me/{username}"
            else:
                invite = await tg_bot.create_chat_invite_link(channel_id)
                resolved_link = invite.invite_link
        except (TelegramBadRequest, TelegramForbiddenError):
            resolved_link = None
        return True, resolved_link
    except (TelegramNetworkError, TelegramRetryAfter) as exc:
        raise storefront_admin.AdminCommandError(
            "external_unknown", "Telegram verification outcome is unknown") from exc
    except (TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError, ValueError):
        return False, None
    finally:
        if tg_bot is not None:
            await tg_bot.session.close()


async def _finalize_channel_claim(
    session: AsyncSession,
    response: Response,
    claim: storefront_admin.ChannelVerification,
    command: storefront_admin.CommandContext,
    token: str,
) -> StorefrontMutationOut[dict[str, Any]]:
    try:
        verified, resolved_link = await _verify_channel_with_telegram(token, claim.channel_id)
    except storefront_admin.AdminCommandError as exc:
        if exc.code != "external_unknown":
            raise
        result = await storefront_admin.mark_external_channel_unknown(
            session, claim, command, error_class=exc.code)
        return _mutation_dict(response, result)
    try:
        result = await storefront_admin.finalize_verified_channel(
            session, claim, command, verified=verified, resolved_link=resolved_link)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


def _plan(raw: dict[str, Any]) -> StorefrontPlanOut:
    return StorefrontPlanOut(**raw)


def _channel(raw: dict[str, Any]) -> StorefrontChannelOut:
    channel_id = raw.get("channel_id")
    required = bool(raw.get("required", raw.get("channel_required", False)))
    configured = bool(channel_id)
    verified_at = raw.get("verified_at")
    verification_error = (
        str(raw["verification_error"]) if raw.get("verification_error") else None
    )
    verified = configured and verified_at is not None and verification_error is None
    health: Literal["ok", "disabled", "error"] = (
        "error" if verification_error else ("ok" if verified and required else "disabled")
    )
    return StorefrontChannelOut(
        channel_id=str(channel_id) if channel_id else None,
        channel_link=str(raw["channel_link"]) if raw.get("channel_link") else None,
        channel_required=required,
        verified=verified,
        health=health,
        verified_at=verified_at,
        verification_error=verification_error,
    )


def _settings(raw: dict[str, Any]) -> StorefrontSettingsOut:
    payment = raw["payment"]
    trial = raw["trial"]
    shop_state = raw["shop_state"]
    return StorefrontSettingsOut(
        payment=StorefrontPaymentSettingsOut(
            pay_card_enabled=payment["card_enabled"],
            pay_usdt_enabled=payment["usdt_enabled"],
            pay_ton_enabled=payment["ton_enabled"],
            card_number=payment.get("card_number"),
            card_holder=payment.get("card_holder"),
            usdt_address=payment.get("usdt_address"),
            ton_address=payment.get("ton_address"),
        ),
        trial=StorefrontTrialSettingsOut(
            free_trial_enabled=trial["enabled"],
            free_trial_gb=trial["gb"],
            free_trial_days=trial["days"],
        ),
        messages=StorefrontMessageSettingsOut(**raw["messages"]),
        shop_state=StorefrontShopStateOut(
            shop_closed=shop_state["closed"], closed_text=shop_state["closed_text"]),
        channel=_channel(raw["channel"]),
        config_version=int(raw["config_version"]),
    )


def _managers(raw: dict[str, Any]) -> StorefrontManagersOut:
    owner = int(raw["owner_telegram_id"])
    coadmins = [int(value) for value in raw["co_admin_ids"]]
    return StorefrontManagersOut(
        owner_id=str(owner),
        items=[
            StorefrontManagerOut(telegram_id=str(owner), role="owner", removable=False),
            *(StorefrontManagerOut(telegram_id=str(value), role="manager", removable=True)
              for value in coadmins),
        ],
        max_count=10,
        config_version=int(raw["config_version"]),
    )


@router.get("", response_model=list[StorefrontSummaryOut])
async def storefronts(
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> list[StorefrontSummaryOut]:
    """List configured shops owned by the authenticated Telegram identity."""
    owned = await list_owned_storefronts(session, ctx)
    # Resolved once; each shop's cost is its own reseller's price, falling back to the global one.
    default_price = await pricing.get_default_price_per_gb(session)
    return [
        storefront_reporting.storefront_summary(
            access,
            cost_per_gb=storefront_pricing.cost_per_gb_with_default(access.reseller, default_price))
        for access in owned
    ]


def _dashboard_dates(
    from_date: dt.date | None, to_date: dt.date | None
) -> tuple[dt.date, dt.date]:
    if (from_date is None) != (to_date is None):
        raise HTTPException(status_code=422, detail="from and to must be supplied together")
    if from_date is None or to_date is None:
        today = dt.datetime.now(storefront_reporting.TEHRAN).date()
        return today.replace(day=1), today
    if to_date == dt.date.max:
        raise HTTPException(status_code=422, detail="to date is outside the supported range")
    span = (to_date - from_date).days + 1
    if span <= 0:
        raise HTTPException(status_code=422, detail="from must be on or before to")
    if span > 366:
        raise HTTPException(status_code=422, detail="date range cannot exceed 366 days")
    return from_date, to_date


@router.get("/{shop_id}/dashboard", response_model=StorefrontDashboardOut)
async def storefront_dashboard(
    from_date: dt.date | None = Query(default=None, alias="from"),
    to_date: dt.date | None = Query(default=None, alias="to"),
    access: StorefrontAccess = Depends(get_storefront_access),
    session: AsyncSession = Depends(get_session),
) -> StorefrontDashboardOut:
    day_from, day_to = _dashboard_dates(from_date, to_date)
    return await storefront_reporting.dashboard(session, access, day_from, day_to)


@router.get("/{shop_id}/finance", response_model=StorefrontFinanceOut)
async def storefront_finance(
    access: StorefrontAccess = Depends(get_storefront_access),
    session: AsyncSession = Depends(get_session),
) -> StorefrontFinanceOut:
    """Every month with activity in one payload, so the page switches months with no refetch."""
    return await storefront_reporting.finance(session, access)


@router.get("/{shop_id}/health", response_model=StorefrontHealthOut)
async def storefront_health(
    access: StorefrontAccess = Depends(get_storefront_access),
    session: AsyncSession = Depends(get_session),
) -> StorefrontHealthOut:
    return await storefront_reporting.health(session, access)


@router.get("/{shop_id}/plans", response_model=StorefrontPlansOut)
async def storefront_plans(
    response: Response,
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontPlansOut:
    try:
        raw = await storefront_admin.list_plans(session, access.shop.id, ctx.chat_id, "portal")
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    version = int(raw["config_version"])
    _set_etag(response, version)
    return StorefrontPlansOut(items=[_plan(item) for item in raw["plans"]], config_version=version)


@router.post(
    "/{shop_id}/plans", response_model=StorefrontMutationOut[StorefrontPlanOut], status_code=201,
)
async def storefront_plan_create(
    body: StorefrontPlanCreate,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[StorefrontPlanOut]:
    command = _command_context(ctx, if_match=if_match, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.create_plan(
            session, access.shop.id, command, **body.model_dump())
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    _check_result(result)
    plan_raw = _result_dict(result).get("plan")
    if not isinstance(plan_raw, dict):
        raise HTTPException(status_code=409, detail={"code": "command_result_unavailable"})
    _set_etag(response, result.config_version)
    return StorefrontMutationOut(result=_plan(plan_raw), config_version=result.config_version)


@router.patch("/{shop_id}/plans/{plan_id}", response_model=StorefrontMutationOut[StorefrontPlanOut])
async def storefront_plan_update(
    plan_id: int,
    body: StorefrontPlanUpdate,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[StorefrontPlanOut]:
    command = _command_context(ctx, if_match=if_match, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.update_plan(
            session, access.shop.id, plan_id, command,
            **body.model_dump(exclude_unset=True),
        )
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    _check_result(result)
    plan_raw = _result_dict(result).get("plan")
    if not isinstance(plan_raw, dict):
        raise HTTPException(status_code=409, detail={"code": "command_result_unavailable"})
    _set_etag(response, result.config_version)
    return StorefrontMutationOut(result=_plan(plan_raw), config_version=result.config_version)


@router.put(
    "/{shop_id}/plans/{plan_id}/enabled",
    response_model=StorefrontMutationOut[StorefrontPlanOut],
)
async def storefront_plan_enabled(
    plan_id: int,
    body: StorefrontEnabledBody,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[StorefrontPlanOut]:
    command = _command_context(ctx, if_match=if_match, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.set_plan_enabled(
            session, access.shop.id, plan_id, command, enabled=body.enabled)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    _check_result(result)
    plan_raw = _result_dict(result).get("plan")
    if not isinstance(plan_raw, dict):
        raise HTTPException(status_code=409, detail={"code": "command_result_unavailable"})
    _set_etag(response, result.config_version)
    return StorefrontMutationOut(result=_plan(plan_raw), config_version=result.config_version)


@router.put(
    "/{shop_id}/plans/order", response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_plan_order(
    body: StorefrontPlanOrderBody,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _command_context(ctx, if_match=if_match, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.reorder_plans(
            session, access.shop.id, command, ordered_ids=body.plan_ids)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.delete(
    "/{shop_id}/plans/{plan_id}", response_model=StorefrontMutationOut[dict[str, int]],
)
async def storefront_plan_delete(
    plan_id: int,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, int]]:
    command = _command_context(ctx, if_match=if_match, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.delete_plan(session, access.shop.id, plan_id, command)
        _check_result(result)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    _set_etag(response, result.config_version)
    return StorefrontMutationOut(result=_result_dict(result), config_version=result.config_version)


@router.get("/{shop_id}/plans/{plan_id}/history", response_model=StorefrontPlanHistoryOut)
async def storefront_plan_history(
    plan_id: int,
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontPlanHistoryOut:
    try:
        items = await storefront_admin.plan_history(
            session, access.shop.id, plan_id, ctx.chat_id, "portal")
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return StorefrontPlanHistoryOut(
        items=[StorefrontPlanHistoryItemOut(
            **{
                **item,
                "actor_telegram_id": (
                    str(item["actor_telegram_id"])
                    if item.get("actor_telegram_id") is not None else None
                ),
            }
        ) for item in items])


@router.get("/{shop_id}/settings", response_model=StorefrontSettingsOut)
async def storefront_settings(
    response: Response,
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontSettingsOut:
    try:
        raw = await storefront_admin.get_settings(session, access.shop.id, ctx.chat_id, "portal")
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    result = _settings(raw)
    _set_etag(response, result.config_version)
    return result


@router.patch(
    "/{shop_id}/settings/payment",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_payment_settings(
    body: StorefrontPaymentSettingsBody,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    field_map = {
        "pay_card_enabled": "card_enabled",
        "pay_usdt_enabled": "usdt_enabled",
        "pay_ton_enabled": "ton_enabled",
        "card_number": "card_number",
        "card_holder": "card_holder",
        "usdt_address": "usdt_address",
        "ton_address": "ton_address",
    }
    changes = {
        field_map[key]: value for key, value in body.model_dump(exclude_unset=True).items()
    }
    command = _command_context(ctx, if_match=if_match, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.update_payment(
            session, access.shop.id, command, **changes)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.patch(
    "/{shop_id}/settings/trial",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_trial_settings(
    body: StorefrontTrialSettingsBody,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _command_context(ctx, if_match=if_match, idempotency_key=idempotency_key)
    # Pass ONLY the fields the client actually sent (the service treats the rest as unchanged via
    # its _UNSET sentinel). Folding current DB state in here would put it in the idempotency hash,
    # so a same-key retry after a concurrent change would 409 instead of cleanly replaying.
    field_map = {"free_trial_enabled": "enabled", "free_trial_gb": "gb", "free_trial_days": "days"}
    changes = {field_map[k]: v for k, v in body.model_dump(exclude_unset=True).items()}
    try:
        result = await storefront_admin.update_trial(
            session, access.shop.id, command, **changes)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.patch(
    "/{shop_id}/settings/messages",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_message_settings(
    body: StorefrontMessageSettingsBody,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _command_context(ctx, if_match=if_match, idempotency_key=idempotency_key)
    # Only the client-sent fields (see the trial handler note) — the schema names match the service.
    changes = body.model_dump(exclude_unset=True)
    try:
        result = await storefront_admin.update_messages(
            session, access.shop.id, command, **changes)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.patch(
    "/{shop_id}/settings/shop-state",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_shop_state_settings(
    body: StorefrontShopStateBody,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _command_context(ctx, if_match=if_match, idempotency_key=idempotency_key)
    # Only the client-sent fields (see the trial handler note).
    field_map = {"shop_closed": "closed", "closed_text": "closed_text"}
    changes = {field_map[k]: v for k, v in body.model_dump(exclude_unset=True).items()}
    try:
        result = await storefront_admin.update_shop_state(
            session, access.shop.id, command, **changes)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.post(
    "/{shop_id}/channel", response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_channel_save(
    body: StorefrontChannelBody,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    """Verify through Telegram without holding a DB transaction, then atomically persist."""
    command = _command_context(ctx, if_match=if_match, idempotency_key=idempotency_key)
    token = storefront.bot_token(access.shop)
    if not token:
        raise HTTPException(status_code=502, detail={"code": "storefront_bot_unavailable"})
    try:
        claim = await storefront_admin.claim_external_channel(
            session, access.shop.id, command, channel_id=body.channel_id)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    if isinstance(claim, storefront_admin.CommandResult):
        return _mutation_dict(response, claim)
    return await _finalize_channel_claim(session, response, claim, command, token)


@router.put(
    "/{shop_id}/channel/enabled", response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_channel_enabled(
    body: StorefrontEnabledBody,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _command_context(ctx, if_match=if_match, idempotency_key=idempotency_key)
    if body.enabled:
        token = storefront.bot_token(access.shop)
        if not token:
            raise HTTPException(status_code=502, detail={"code": "storefront_bot_unavailable"})
        try:
            claim = await storefront_admin.claim_external_channel_enable(
                session, access.shop.id, command)
        except storefront_admin.AdminCommandError as exc:
            _raise_admin_error(exc)
        if isinstance(claim, storefront_admin.CommandResult):
            return _mutation_dict(response, claim)
        return await _finalize_channel_claim(session, response, claim, command, token)
    try:
        result = await storefront_admin.set_channel_required(
            session, access.shop.id, command, required=body.enabled)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.delete(
    "/{shop_id}/channel", response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_channel_delete(
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _command_context(ctx, if_match=if_match, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.delete_channel(session, access.shop.id, command)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.get("/{shop_id}/managers", response_model=StorefrontManagersOut)
async def storefront_managers(
    response: Response,
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontManagersOut:
    try:
        raw = await storefront_admin.list_managers(
            session, access.shop.id, ctx.chat_id, "portal")
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    result = _managers(raw)
    _set_etag(response, result.config_version)
    return result


@router.post(
    "/{shop_id}/managers", response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_manager_add(
    body: StorefrontManagerBody,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _command_context(ctx, if_match=if_match, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.add_manager(
            session, access.shop.id, command, telegram_id=body.telegram_id)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.delete(
    "/{shop_id}/managers/{telegram_id}",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_manager_remove(
    telegram_id: int,
    response: Response,
    if_match: str = Header(alias="If-Match"),
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _command_context(ctx, if_match=if_match, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.remove_manager(
            session, access.shop.id, command, telegram_id=telegram_id)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.get("/{shop_id}/preview", response_model=StorefrontCustomerPreviewOut)
async def storefront_preview(
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontCustomerPreviewOut:
    try:
        raw = await storefront_admin.customer_preview(
            session, access.shop.id, ctx.chat_id, "portal")
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    methods = [name for name, enabled in raw["payment_methods"].items() if enabled]
    return StorefrontCustomerPreviewOut(
        welcome_text=raw["welcome_text"],
        support_contact=raw["support_contact"],
        shop_closed=raw["shop_closed"],
        closed_text=raw["closed_text"],
        free_trial=StorefrontPreviewFreeTrialOut(
            enabled=raw["trial"]["enabled"],
            gb=raw["trial"]["gb"],
            days=raw["trial"]["days"],
        ),
        payment_methods=methods,
        enabled_plans=[StorefrontPreviewPlanOut(
            id=item["id"], title=item["title"], gb=item["gb"], days=item["days"],
            price_toman=item["price_toman"],
        ) for item in raw["plans"]],
        channel_required=bool(raw.get("channel_required", False)),
    )


# ── customer & order management (plan 004) ───────────────────────────────────
_LEDGER_KINDS = {
    "topup", "purchase", "manual_credit", "manual_debit", "refund", "renew_reversal", "credit_bonus",
}
_LEDGER_STATUSES = {"pending", "confirmed", "rejected", "done"}
_ORDER_STATUSES = {"pending", "provisioned", "disabled", "renewing", "failed", "deleted"}


def _entity_command_context(
    ctx: ResellerContext, *, idempotency_key: str,
) -> storefront_admin.CommandContext:
    """Command context for entity (customer/order) mutations: Idempotency-Key only, no If-Match —
    they are not shop-config changes, so `expected_version` is an unused placeholder."""
    key = idempotency_key.strip()
    if not key or len(key) > 128:
        raise HTTPException(status_code=422, detail={"code": "invalid_idempotency_key"})
    return storefront_admin.CommandContext(
        actor_telegram_id=ctx.chat_id, actor_role="owner", source="portal",
        idempotency_key=key, expected_version=1, correlation_id=str(uuid.uuid4()),
    )


def _bad(code: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": code})


@router.get("/{shop_id}/customers")
async def storefront_customers_list(
    shop_id: int,
    q: str | None = Query(None),
    banned: bool | None = Query(None),
    activity: str | None = Query(None),
    has_service: bool | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(25, ge=1, le=100),
    access: StorefrontAccess = Depends(get_storefront_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if q is not None and not q.strip().isdigit() and len(q.strip()) < 3:
        raise _bad("query_too_short")
    if activity is not None and activity not in ("active30", "inactive30"):
        raise _bad("invalid_activity")
    try:
        items, next_cursor = await storefront_customers.list_customers_keyset(
            session, access.shop.id, q=q, banned=banned, activity=activity,
            has_service=has_service, cursor=cursor, limit=limit)
    except storefront_cursor.CursorError:
        raise _bad("invalid_cursor") from None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/{shop_id}/customers/{customer_id}")
async def storefront_customer_detail(
    shop_id: int, customer_id: int,
    access: StorefrontAccess = Depends(get_storefront_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    detail = await storefront_customers.customer_detail(session, access.shop.id, customer_id)
    if detail is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return detail


@router.get("/{shop_id}/customers/{customer_id}/ledger")
async def storefront_customer_ledger(
    shop_id: int, customer_id: int,
    kind: str | None = Query(None),
    status: str | None = Query(None),
    date_from: dt.date | None = Query(None, alias="from"),
    date_to: dt.date | None = Query(None, alias="to"),
    cursor: str | None = Query(None),
    limit: int = Query(25, ge=1, le=100),
    access: StorefrontAccess = Depends(get_storefront_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if kind is not None and kind not in _LEDGER_KINDS:
        raise _bad("invalid_kind")
    if status is not None and status not in _LEDGER_STATUSES:
        raise _bad("invalid_status")
    if date_from is not None and date_to is not None and (date_to - date_from).days > 366:
        raise _bad("date_span_too_large")
    dt_from = dt.datetime.combine(date_from, dt.time.min, dt.timezone.utc) if date_from else None
    dt_to = dt.datetime.combine(date_to, dt.time.max, dt.timezone.utc) if date_to else None
    try:
        result = await storefront_customers.customer_ledger(
            session, access.shop.id, customer_id, kind=kind, status=status,
            dt_from=dt_from, dt_to=dt_to, cursor=cursor, limit=limit)
    except storefront_cursor.CursorError:
        raise _bad("invalid_cursor") from None
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    items, next_cursor = result
    return {"items": items, "next_cursor": next_cursor}


@router.get("/{shop_id}/customers/{customer_id}/orders")
async def storefront_customer_orders(
    shop_id: int, customer_id: int,
    status: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(25, ge=1, le=100),
    access: StorefrontAccess = Depends(get_storefront_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if status is not None and status not in _ORDER_STATUSES:
        raise _bad("invalid_status")
    try:
        result = await storefront_customers.list_customer_orders_keyset(
            session, access.shop.id, customer_id, status=status, cursor=cursor, limit=limit)
    except storefront_cursor.CursorError:
        raise _bad("invalid_cursor") from None
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    items, next_cursor = result
    return {"items": items, "next_cursor": next_cursor}


@router.get("/{shop_id}/orders/{order_id}")
async def storefront_order_detail(
    shop_id: int, order_id: int,
    access: StorefrontAccess = Depends(get_storefront_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    detail = await storefront_customers.order_detail(session, access.shop.id, order_id)
    if detail is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return detail


@router.patch(
    "/{shop_id}/customers/{customer_id}/status",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_customer_status(
    shop_id: int, customer_id: int,
    body: StorefrontCustomerStatusBody,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _entity_command_context(ctx, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.set_customer_banned(
            session, access.shop.id, customer_id, command, banned=body.banned, reason=body.reason)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.post("/{shop_id}/orders/{order_id}/refresh")
async def storefront_order_refresh(
    shop_id: int, order_id: int,
    access: StorefrontAccess = Depends(get_storefront_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        await storefront_admin._owned_order(session, access.shop.id, order_id)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    window = 30
    try:
        window = max(5, min(3600, int(
            await settings_service.get(session, "storefront_live_refresh_seconds", 30) or 30)))
    except (TypeError, ValueError):
        window = 30
    try:
        status, freshness = await storefront_provision.refresh_order_live(
            order_id=order_id, window_seconds=window)
    except storefront_provision.RateLimited as exc:
        raise HTTPException(
            status_code=429, detail={"code": "rate_limited"},
            headers={"Retry-After": str(exc.retry_after)}) from exc
    return {"status": status, "freshness": freshness}


@router.post(
    "/{shop_id}/orders/{order_id}/renew",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_order_renew(
    shop_id: int, order_id: int,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _entity_command_context(ctx, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.renew_order(session, access.shop.id, order_id, command)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.put(
    "/{shop_id}/orders/{order_id}/enabled",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_order_enabled(
    shop_id: int, order_id: int,
    body: StorefrontOrderEnabledBody,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _entity_command_context(ctx, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.set_order_enabled(
            session, access.shop.id, order_id, command, enabled=body.enabled)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.delete(
    "/{shop_id}/orders/{order_id}",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_order_delete(
    shop_id: int, order_id: int,
    body: StorefrontOrderDeleteBody,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _entity_command_context(ctx, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.delete_order(
            session, access.shop.id, order_id, command, reason=body.reason)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


# ── wallet & top-up operations center (plan 005) ─────────────────────────────
_TOPUP_STATUSES = {"pending", "confirmed", "rejected"}
_TOPUP_METHODS = {"card", "usdt", "ton"}
_PROOF_ROOT = "data/storefront_proofs"


@router.get("/{shop_id}/topups")
async def storefront_topups_list(
    shop_id: int,
    status: str | None = Query(None),
    method: str | None = Query(None),
    min_amount: int | None = Query(None, ge=1, le=10**12),
    max_amount: int | None = Query(None, ge=1, le=10**12),
    date_from: dt.date | None = Query(None, alias="from"),
    date_to: dt.date | None = Query(None, alias="to"),
    q: str | None = Query(None),
    cursor: str | None = Query(None),
    limit: int = Query(25, ge=1, le=100),
    access: StorefrontAccess = Depends(get_storefront_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if status is not None and status not in _TOPUP_STATUSES:
        raise _bad("invalid_status")
    if method is not None and method not in _TOPUP_METHODS:
        raise _bad("invalid_method")
    if q is not None and not q.strip().isdigit() and len(q.strip()) < 3:
        raise _bad("query_too_short")
    if date_from is not None and date_to is not None and (date_to - date_from).days > 366:
        raise _bad("date_span_too_large")
    dt_from = dt.datetime.combine(date_from, dt.time.min, dt.timezone.utc) if date_from else None
    dt_to = dt.datetime.combine(date_to, dt.time.max, dt.timezone.utc) if date_to else None
    try:
        items, next_cursor = await storefront_customers.list_topups_keyset(
            session, access.shop.id, status=status, method=method, min_amount=min_amount,
            max_amount=max_amount, dt_from=dt_from, dt_to=dt_to, q=q, cursor=cursor, limit=limit)
    except storefront_cursor.CursorError:
        raise _bad("invalid_cursor") from None
    return {"items": items, "next_cursor": next_cursor, "total_hint": None}


@router.get("/{shop_id}/topups/{txn_id}")
async def storefront_topup_detail(
    shop_id: int, txn_id: int,
    access: StorefrontAccess = Depends(get_storefront_access),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    detail = await storefront_customers.topup_detail(session, access.shop.id, txn_id)
    if detail is None:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    return detail


@router.get("/{shop_id}/topups/{txn_id}/proof")
async def storefront_topup_proof(
    shop_id: int, txn_id: int,
    access: StorefrontAccess = Depends(get_storefront_access),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    txn = await session.get(StorefrontWalletTxn, txn_id)
    # Uniform 404 for foreign / non-topup / no-proof / missing-file / path-escape — no existence leak,
    # and NEVER derive authorization from the filename.
    if txn is None or txn.storefront_bot_id != access.shop.id or txn.kind != "topup" or not txn.proof_path:
        raise HTTPException(status_code=404, detail={"code": "not_found"})
    root = os.path.realpath(_PROOF_ROOT)
    real = os.path.realpath(txn.proof_path)
    if not real.startswith(root + os.sep) or not os.path.isfile(real):
        raise HTTPException(status_code=404, detail={"code": "not_found"})

    def _iter():  # noqa: ANN202
        with open(real, "rb") as fh:
            while chunk := fh.read(65536):
                yield chunk

    return StreamingResponse(
        _iter(), media_type="image/jpeg",
        headers={"Content-Disposition": f'inline; filename="proof_{txn_id}.jpg"'})


@router.post(
    "/{shop_id}/topups/{txn_id}/decision",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_topup_decision(
    shop_id: int, txn_id: int,
    body: StorefrontTopupDecisionBody,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _entity_command_context(ctx, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.set_topup_decision(
            session, access.shop.id, txn_id, command,
            decision=body.decision, corrected_amount=body.corrected_amount, reason=body.reason)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.post(
    "/{shop_id}/topups/bulk-decisions",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_topups_bulk(
    shop_id: int,
    body: StorefrontBulkDecisionBody,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _entity_command_context(ctx, idempotency_key=idempotency_key)
    items = [{"txn_id": i.txn_id, "decision": i.decision} for i in body.items]
    try:
        result = await storefront_admin.bulk_topup_decisions(
            session, access.shop.id, command, items=items, reason=body.reason)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.post(
    "/{shop_id}/customers/{customer_id}/wallet-adjustments",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_wallet_adjustment(
    shop_id: int, customer_id: int,
    body: StorefrontWalletAdjustmentBody,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _entity_command_context(ctx, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.adjust_wallet(
            session, access.shop.id, customer_id, command,
            amount_toman_signed=body.amount_toman_signed, reason=body.reason)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


# ─────────────────────────── credit codes ────────────────────────────────────

@router.get("/{shop_id}/credits")
async def storefront_credits_list(
    shop_id: int,
    include_archived: bool = Query(False),
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        return await storefront_admin.list_credits(
            session, access.shop.id, ctx.chat_id, "portal",
            include_archived=include_archived, cursor=cursor, limit=limit)
    except storefront_cursor.CursorError:
        raise _bad("invalid_cursor") from None
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)


@router.post(
    "/{shop_id}/credits",
    response_model=StorefrontMutationOut[dict[str, Any]], status_code=201,
)
async def storefront_credit_create(
    shop_id: int,
    body: StorefrontCreditCreate,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _entity_command_context(ctx, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.create_credit(
            session, access.shop.id, command,
            code=body.code, kind=body.kind, percent_off=body.percent_off,
            amount_toman=body.amount_toman, max_bonus_toman=body.max_bonus_toman,
            min_topup_toman=body.min_topup_toman, is_gift=body.is_gift,
            max_uses=body.max_uses, per_customer_limit=body.per_customer_limit,
            starts_at=body.starts_at, expires_at=body.expires_at)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    out = _mutation_dict(response, result)
    response.status_code = result.response_status
    return out


@router.patch(
    "/{shop_id}/credits/{code_id}",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_credit_update(
    shop_id: int, code_id: int,
    body: StorefrontCreditUpdate,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _entity_command_context(ctx, idempotency_key=idempotency_key)
    changes = body.model_dump(exclude_unset=True)
    try:
        result = await storefront_admin.update_credit(
            session, access.shop.id, code_id, command, changes=changes)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.put(
    "/{shop_id}/credits/{code_id}/enabled",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_credit_set_enabled(
    shop_id: int, code_id: int,
    body: StorefrontEnabledBody,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _entity_command_context(ctx, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.set_credit_enabled(
            session, access.shop.id, code_id, command, enabled=body.enabled)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.post(
    "/{shop_id}/credits/{code_id}/archive",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_credit_archive(
    shop_id: int, code_id: int,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _entity_command_context(ctx, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.archive_credit(
            session, access.shop.id, code_id, command)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.get("/{shop_id}/credits/{code_id}/usage")
async def storefront_credit_usage(
    shop_id: int, code_id: int,
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        return await storefront_admin.credit_usage(
            session, access.shop.id, code_id, ctx.chat_id, "portal")
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)


@router.get("/{shop_id}/credits/{code_id}/redemptions")
async def storefront_credit_redemptions(
    shop_id: int, code_id: int,
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        return await storefront_admin.list_credit_redemptions(
            session, access.shop.id, code_id, ctx.chat_id, "portal", cursor=cursor, limit=limit)
    except storefront_cursor.CursorError:
        raise _bad("invalid_cursor") from None
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)


# ─────────────────────── communications (broadcast + direct) ──────────────────

@router.get("/{shop_id}/audience/preview")
async def storefront_audience_preview(
    shop_id: int,
    segment: str = Query(...),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        return await storefront_admin.preview_audience(
            session, access.shop.id, segment, ctx.chat_id, "portal")
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)


@router.get("/{shop_id}/broadcasts")
async def storefront_broadcasts_list(
    shop_id: int,
    cursor: str | None = Query(None),
    limit: int = Query(25, ge=1, le=100),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        return await storefront_admin.list_broadcasts(
            session, access.shop.id, ctx.chat_id, "portal", cursor=cursor, limit=limit)
    except storefront_cursor.CursorError:
        raise _bad("invalid_cursor") from None
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)


@router.post(
    "/{shop_id}/broadcasts",
    response_model=StorefrontMutationOut[dict[str, Any]], status_code=202,
)
async def storefront_broadcast_create(
    shop_id: int,
    body: StorefrontBroadcastBody,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _entity_command_context(ctx, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.enqueue_broadcast(
            session, access.shop.id, command, segment=body.segment, text=body.text)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    out = _mutation_dict(response, result)
    response.status_code = result.response_status
    return out


@router.get("/{shop_id}/broadcasts/{job_id}")
async def storefront_broadcast_status(
    shop_id: int, job_id: int,
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        return await storefront_admin.get_broadcast(
            session, access.shop.id, job_id, ctx.chat_id, "portal")
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)


@router.post(
    "/{shop_id}/broadcasts/{job_id}/cancel",
    response_model=StorefrontMutationOut[dict[str, Any]],
)
async def storefront_broadcast_cancel(
    shop_id: int, job_id: int,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _entity_command_context(ctx, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.cancel_broadcast(
            session, access.shop.id, job_id, command)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    return _mutation_dict(response, result)


@router.post(
    "/{shop_id}/customers/{customer_id}/message",
    response_model=StorefrontMutationOut[dict[str, Any]], status_code=202,
)
async def storefront_customer_message(
    shop_id: int, customer_id: int,
    body: StorefrontDirectMessageBody,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    access: StorefrontAccess = Depends(get_storefront_access),
    ctx: ResellerContext = Depends(get_current_reseller),
    session: AsyncSession = Depends(get_session),
) -> StorefrontMutationOut[dict[str, Any]]:
    command = _entity_command_context(ctx, idempotency_key=idempotency_key)
    try:
        result = await storefront_admin.enqueue_direct(
            session, access.shop.id, customer_id, command, text=body.text)
    except storefront_admin.AdminCommandError as exc:
        _raise_admin_error(exc)
    out = _mutation_dict(response, result)
    response.status_code = result.response_status
    return out
