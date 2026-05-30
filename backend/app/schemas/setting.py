from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class SettingOut(BaseModel):
    key: str
    value: Any
    is_secret: bool
    group: str
    has_value: bool


class SettingUpdate(BaseModel):
    key: str
    value: Any


class SettingsBulkUpdate(BaseModel):
    items: list[SettingUpdate]
