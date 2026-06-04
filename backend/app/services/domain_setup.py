"""
Set the panel's public domain at runtime and make Caddy obtain an HTTPS cert for it.

How it works: the backend talks to Caddy's admin API (http://caddy:2019) and rewrites
the single site's address to the new domain. Caddy then fetches a Let's Encrypt cert
automatically. The domain is also persisted to the repo-root .env so it survives a
full `docker compose` restart (where Caddy reads SERVER_DOMAIN from the env).

No-op friendly: if Caddy isn't reachable (e.g. local dev without Caddy), it still
saves the setting and returns a clear message.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import settings_service

log = logging.getLogger("domain_setup")

CADDY_ADMIN = os.environ.get("CADDY_ADMIN", "http://caddy:2019")
# The repo-root .env (backend runs from /app, repo .env is mounted/!readable in prod).
ENV_PATHS = [Path("/app/.env"), Path(__file__).resolve().parents[3] / ".env"]

_DOMAIN_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})+$")
# Anchored, with char classes that exclude whitespace/newlines — so a value that passes can
# never inject a second line into .env (env-injection via the unauthenticated setup wizard).
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _valid_domain(d: str) -> bool:
    return bool(_DOMAIN_RE.match(d or ""))


def _valid_email(e: str) -> bool:
    return bool(_EMAIL_RE.match(e or ""))


def _persist_env(domain: str, email: str | None) -> bool:
    """Write SERVER_DOMAIN (+ ACME_EMAIL) into .env so restarts keep the domain."""
    for p in ENV_PATHS:
        try:
            if not p.exists():
                continue
            text = p.read_text()
            def setkv(t: str, k: str, v: str) -> str:
                v = re.sub(r"[\r\n]", "", v)  # never let a value break onto a new .env line
                if re.search(rf"^{k}=.*$", t, flags=re.M):
                    return re.sub(rf"^{k}=.*$", f"{k}={v}", t, flags=re.M)
                return t + (("" if t.endswith("\n") else "\n") + f"{k}={v}\n")
            text = setkv(text, "SERVER_DOMAIN", domain)
            if email:
                text = setkv(text, "ACME_EMAIL", email)
            p.write_text(text)
            return True
        except Exception:  # noqa: BLE001
            log.warning("could not write %s", p, exc_info=True)
    return False


async def _caddy_set_domain(domain: str, email: str | None) -> tuple[bool, str]:
    """Reconfigure the running Caddy to serve `domain` with automatic HTTPS."""
    config = {
        "admin": {"listen": "0.0.0.0:2019"},
        "apps": {
            "http": {
                "servers": {
                    "srv0": {
                        "listen": [":443", ":80"],
                        "routes": [
                            {
                                # The domain: proxy API → backend, everything else → SPA.
                                "match": [{"host": [domain]}],
                                "handle": [{
                                    "handler": "subroute",
                                    "routes": [
                                        {
                                            "match": [{"path": ["/api/*", "/health", "/docs", "/openapi.json"]}],
                                            "handle": [{"handler": "reverse_proxy",
                                                        "upstreams": [{"dial": "backend:8000"}]}],
                                        },
                                        {
                                            "handle": [{"handler": "reverse_proxy",
                                                        "upstreams": [{"dial": "frontend:80"}]}],
                                        },
                                    ],
                                }],
                            },
                            {
                                # Anything else (bare IP, other host) → permanent redirect
                                # to the domain, so the IP address is no longer usable.
                                "handle": [{
                                    "handler": "static_response",
                                    "status_code": 308,
                                    "headers": {"Location": [f"https://{domain}{{http.request.uri}}"]},
                                }],
                            },
                        ],
                    }
                }
            }
        },
    }
    if email:
        config["apps"]["tls"] = {
            "automation": {"policies": [{"subjects": [domain], "issuers": [
                {"module": "acme", "email": email}
            ]}]}
        }
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{CADDY_ADMIN}/load", json=config)
            if r.status_code in (200, 201):
                return True, "ok"
            return False, f"caddy admin returned {r.status_code}: {r.text[:200]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"caddy unreachable: {exc}"


async def set_domain(session: AsyncSession, domain: str, email: str | None = None) -> dict:
    domain = (domain or "").strip().lower().rstrip("/")
    domain = re.sub(r"^https?://", "", domain)
    if not _valid_domain(domain):
        return {"ok": False, "error": "دامنهٔ واردشده معتبر نیست."}

    email = (email or await settings_service.get(session, "acme_email", "") or "").strip() or None
    if email and not _valid_email(email):
        return {"ok": False, "error": "ایمیل واردشده معتبر نیست."}

    # persist to settings (panel) + .env (survives restart)
    await settings_service.set_value(session, "server_domain", domain)
    if email:
        await settings_service.set_value(session, "acme_email", email)
    persisted = _persist_env(domain, email)

    applied, msg = await _caddy_set_domain(domain, email)
    # Only flag HTTPS as live (and thus lock the IP→domain redirect) when Caddy
    # actually accepted the config. On failure we stay on the IP and let the user retry.
    await settings_service.set_value(session, "https_enabled", bool(applied))
    return {
        "ok": applied,
        "domain": domain,
        "url": f"https://{domain}",
        "persisted_env": persisted,
        "detail": msg,
        "message": (
            f"دامنه ثبت شد. تا چند لحظه دیگر گواهی SSL گرفته می‌شود؛ سپس از طریق https://{domain} وارد شوید."
            if applied else
            f"دامنه ذخیره شد اما اعمال زندهٔ آن ناموفق بود ({msg}). پس از یک‌بار ری‌استارت سرویس‌ها فعال می‌شود."
        ),
    }
