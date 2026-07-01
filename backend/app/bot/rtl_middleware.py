"""Apply `rtl()` to every OUTGOING Telegram message centrally (I04).

The central helpers (owner_notify / notifier / invoice delivery) already pass text
through `app.bot.rtl.rtl`, but the ~200 direct `message.answer(...)` calls across the
handlers don't — any of them mixing Persian with Latin/digits (amounts, TXIDs, panel
keys) renders jumbled. Instead of touching every call site (and every future one),
this client-session request middleware rewrites `text`/`caption` on the send/edit
methods just before the API call. `rtl()` is idempotent and HTML-aware, so messages
already processed by the central helpers pass through unchanged.

`AnswerCallbackQuery` (toast alerts) is deliberately NOT included for now — see the
"Owner decisions pending" list in docs/IMPROVEMENT_PLAN.md.

Installed at all three Bot construction sites: the main bot loop (`app/bot/run.py`),
`app/bot/telegram.py::build_bot` (notifier/owner paths), and the storefront manager.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.methods import (
    EditMessageCaption,
    EditMessageText,
    SendAnimation,
    SendAudio,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVideo,
    SendVoice,
)
from aiogram.methods.base import Response, TelegramMethod, TelegramType

from app.bot.rtl import rtl

if TYPE_CHECKING:
    from aiogram import Bot

log = logging.getLogger("bot.rtl")

_TEXT_METHODS = (SendMessage, EditMessageText)
_CAPTION_METHODS = (
    SendPhoto, SendDocument, SendVideo, SendAnimation, SendAudio, EditMessageCaption,
    SendVoice,
)


class RtlRequestMiddleware(BaseRequestMiddleware):
    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        try:
            if isinstance(method, _TEXT_METHODS) and method.text:
                method = cast(
                    "TelegramMethod[TelegramType]",
                    method.model_copy(update={"text": rtl(method.text)}),
                )
            elif isinstance(method, _CAPTION_METHODS) and method.caption:
                method = cast(
                    "TelegramMethod[TelegramType]",
                    method.model_copy(update={"caption": rtl(method.caption)}),
                )
        except Exception:  # noqa: BLE001 - formatting must never block sending
            log.warning("rtl request middleware failed; sending original", exc_info=True)
        return await make_request(bot, method)


def install(bot: Bot) -> None:
    """Attach the middleware to a Bot's client session."""
    bot.session.middleware(RtlRequestMiddleware())
