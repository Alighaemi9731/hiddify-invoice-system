"""
Parse a reseller's pasted panel link and match it to a reseller record.

Match key = the UUID in the link (the reseller's admin uuid). We also capture the
host, path, and the #tag. Example:
    https://panel-01.example.com/sXDm8ZxnkWl5kI4RppukoBGLx8E/e5ed5732-...-269d254e5c49/#PH
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@dataclass
class ParsedLink:
    uuid: str
    host: str | None
    path: str | None
    tag: str | None


def normalize_host(value: str | None) -> str | None:
    """Normalize a panel host for identity matching.

    Hostnames are case-insensitive; a trailing dot and the default HTTPS port do not
    change identity. Non-default ports remain significant.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw if "://" in raw else f"//{raw}", scheme="https")
    hostname = parsed.hostname
    if not hostname:
        return None
    try:
        hostname = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        hostname = hostname.rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError:
        return None
    if port and port != 443:
        return f"{hostname}:{port}"
    return hostname


def normalize_path(value: str | None) -> str | None:
    """Normalize the secret proxy path while preserving its case-sensitive content."""
    raw = unquote(value or "").strip()
    parts = [part for part in raw.split("/") if part]
    return "/".join(parts) or None


def parse_link(text: str) -> ParsedLink | None:
    """Extract uuid (+ host, path, #tag) from any text containing a panel link."""
    if not text:
        return None
    m = _UUID_RE.search(text)
    if not m:
        return None
    uuid = m.group(0).lower()

    host = None
    path = None
    tag = None

    # Find the URL token that contains THIS uuid. A message may contain another unrelated
    # URL; pairing its host with a uuid elsewhere in the text would create a false identity.
    token = None
    for candidate in re.findall(r"https?://[^\s]+", text, flags=re.I):
        if uuid in unquote(candidate).lower():
            token = candidate
            break
    if token is None:
        # Bare host/path without a scheme, but still require the token to contain the uuid.
        bare = re.search(
            rf"([\w.-]+\.[a-zA-Z]{{2,}}(?::\d+)?/[^\s]*{re.escape(uuid)}[^\s]*)",
            text,
            flags=re.I,
        )
        if bare:
            token = "https://" + bare.group(0)
    if token:
        token = token.rstrip(").,]")
        if "#" in token:
            raw_tag = token.split("#", 1)[1].strip()
            # The fragment is often percent-encoded (e.g. Persian names) — decode it.
            tag = unquote(raw_tag) or None if raw_tag else None
            token = token.split("#", 1)[0]
        parsed = urlsplit(token)
        host = normalize_host(token)
        # The complete path BEFORE the uuid is the panel's proxy path. It can contain more
        # than one segment, so selecting the first non-uuid segment is not sufficient.
        segs = [s for s in parsed.path.split("/") if s]
        uuid_idx = next((i for i, seg in enumerate(segs) if unquote(seg).lower() == uuid), None)
        path = normalize_path("/".join(segs[:uuid_idx])) if uuid_idx is not None else None
    return ParsedLink(uuid=uuid, host=host, path=path, tag=tag)
