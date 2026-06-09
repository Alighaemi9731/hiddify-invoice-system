from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SettingOut(BaseModel):
    key: str
    value: Any
    is_secret: bool
    group: str
    has_value: bool


class SettingUpdate(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    value: Any


class SettingsBulkUpdate(BaseModel):
    items: list[SettingUpdate] = Field(min_length=1, max_length=200)
