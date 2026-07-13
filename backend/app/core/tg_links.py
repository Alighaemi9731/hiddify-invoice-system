"""Build a "open this Telegram user's private chat / profile" deep link — the single source of
truth shared by the storefront customer button and the main-bot reseller-name link.

Precedence mirrors the frontend `telegramHref` (frontend/src/components/TelegramLink.tsx):
a @username → `https://t.me/<username>` (most reliable), else a numeric id → `tg://user?id=<id>`
(works because the relevant bot has already interacted with the user), else None.

`tg://user?id=` is valid both as an HTML `<a>` link AND as an inline-keyboard button `url` — the
repo already ships one as a button url (app/bot/handlers/owner.py + app/bot/keyboards.py), so aiogram
accepts it.
"""
from __future__ import annotations

import re

# A Telegram username is 5–32 chars of [A-Za-z0-9_]. Validate before building a t.me URL so a
# malformed value can't make Telegram reject the whole inline keyboard / message.
_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{4,32}")


def clean_username(username: str | None) -> str | None:
    """A validated bare Telegram username (no @) suitable for a t.me/<u> link, or None."""
    u = (username or "").strip().lstrip("@")
    return u if _USERNAME_RE.fullmatch(u) else None


def tg_pv_url(username: str | None, chat_id: int | None) -> str | None:
    """Best link to open a user's PV/profile: t.me/<username> if a valid username, else
    tg://user?id=<chat_id>, else None (no usable identifier)."""
    u = clean_username(username)
    if u:
        return f"https://t.me/{u}"
    if chat_id:
        return f"tg://user?id={chat_id}"
    return None
