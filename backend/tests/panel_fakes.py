"""Shared fakes for the Hiddify write adapter (`AdminApiClient`).

Enforcement resolves a target user with `get_user_identity`, which returns the panel's numeric
rowid **and the admin that currently owns the user** in the one call it was already making. The
owner matters because a queued action carries a user→owner map frozen at queue time, and Hiddify
re-parents a deleted sub-admin's users to its parent.

`as_identity` adapts the ubiquitous "uuid → rowid" fake to that contract. `owner=None` means "the
panel says nothing that contradicts the action's own map" — the normal case, and what every test
not specifically about re-parenting intends.
"""
from __future__ import annotations

from app.services.panel_client.admin_api import UserIdentity


def as_identity(fake_get_user_id, owner: str | None = None):
    """Wrap a `get_user_id`-shaped fake so it satisfies `get_user_identity`."""

    async def _fake(self, panel, user_uuid, *, api_key=None):  # noqa: ANN001
        uid = await fake_get_user_id(self, panel, user_uuid, api_key=api_key)
        return None if uid is None else UserIdentity(uid, owner)

    return _fake
