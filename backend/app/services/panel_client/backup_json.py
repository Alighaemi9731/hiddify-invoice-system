"""Read adapter: download a panel's backup JSON from /admin/backup/backupfile/."""
from __future__ import annotations

import logging

import httpx

from app.services.panel_client.base import PanelClient, PanelData, parse_backup

log = logging.getLogger("panel.backup_json")


class BackupJsonClient(PanelClient):
    """
    Fetches the full backup document for a panel. The owner UUID embedded in the
    URL path authenticates the request; some panel versions also accept it as the
    HTTP basic-auth username, so we fall back to that on 401/403.
    """

    def __init__(self, timeout: float = 45.0) -> None:
        self.timeout = timeout

    async def fetch_backup(self, panel) -> PanelData:  # noqa: ANN001
        uuid = panel.owner_uuid
        in_path = f"{panel.base_secret_url}/admin/backup/backupfile/"
        # Ordered by what works on current Hiddify: uuid removed from the path and
        # used as the basic-auth username. Other shapes are tried as fallbacks.
        candidates = [
            (panel.backup_url, (uuid, "")),  # proxy/admin/backup, basic-auth=uuid  ← primary
            (in_path, None),                 # uuid in path, no auth
            (in_path, (uuid, "")),           # uuid in path + basic-auth
            (panel.backup_url, None),        # proxy/admin/backup, no auth
        ]
        last = "no response"
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, verify=True
        ) as client:
            for url, auth in candidates:
                try:
                    resp = await client.get(url, auth=auth)
                except Exception as exc:  # noqa: BLE001
                    last = f"{type(exc).__name__}: {exc}"
                    continue
                if resp.status_code != 200:
                    last = f"HTTP {resp.status_code}"
                    continue
                try:
                    payload = resp.json()
                except Exception:  # noqa: BLE001
                    last = f"non-JSON response (content-type={resp.headers.get('content-type')})"
                    continue
                # A valid Hiddify backup carries BOTH collections. Requiring only ONE let a
                # truncated/partial response through, which on sync looked like every user (or
                # every admin) had vanished — mass "deletion" and wrong billing. Require both,
                # as lists, and a non-empty admin set (every panel has at least the owner admin).
                if isinstance(payload, dict) and isinstance(payload.get("admin_users"), list) \
                        and isinstance(payload.get("users"), list):
                    data = parse_backup(payload)
                    if not data.admins:
                        last = "backup has no admins (truncated/partial?)"
                        continue
                    log.info(
                        "Fetched backup for panel '%s': %d admins, %d users",
                        panel.key, len(data.admins), len(data.users),
                    )
                    return data
                last = "JSON missing admin_users/users list(s)"
        raise RuntimeError(f"could not fetch backup ({last})")
