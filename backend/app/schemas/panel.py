from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class PanelCreate(BaseModel):
    key: str = Field(min_length=1, max_length=32)
    name: str = ""
    host: str = Field(min_length=1, description="e.g. panel-01.example.com (no scheme)")
    proxy_path: str = Field(min_length=1, description="secret URL path segment")
    owner_uuid: str = Field(min_length=8, description="panel super-admin uuid")
    admin_api_key: str | None = None
    enabled: bool = True


class PanelUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    proxy_path: str | None = None
    owner_uuid: str | None = None
    admin_api_key: str | None = None
    enabled: bool | None = None


class PanelOut(BaseModel):
    id: int
    key: str
    name: str
    host: str
    owner_uuid: str
    enabled: bool
    status: str
    source: str
    proxy_path_masked: str
    has_admin_api_key: bool
    last_synced_at: dt.datetime | None
    last_error: str | None
    backup_url: str
    resellers_count: int = 0
    end_users_count: int = 0


class SyncRunOut(BaseModel):
    id: int
    panel_id: int | None
    source: str
    status: str
    admin_count: int
    user_count: int
    error: str | None
    started_at: dt.datetime | None
    finished_at: dt.datetime | None


class SyncTestResult(BaseModel):
    ok: bool
    admin_count: int = 0
    user_count: int = 0
    error: str | None = None
