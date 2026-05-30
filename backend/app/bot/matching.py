"""
Parse a reseller's pasted panel link and match it to a reseller record.

Match key = the UUID in the link (the reseller's admin uuid). We also capture the
host, path, and the #tag. Example:
    https://panel-01.example.com/sXDm8ZxnkWl5kI4RppukoBGLx8E/e5ed5732-...-269d254e5c49/#PH
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@dataclass
class ParsedLink:
    uuid: str
    host: str | None
    path: str | None
    tag: str | None


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

    # Find a URL-ish token to extract host/path.
    url_match = re.search(r"https?://[^\s]+", text)
    token = url_match.group(0) if url_match else None
    if token is None:
        # bare host/path without scheme — try to reconstruct
        bare = re.search(r"([\w.-]+\.[a-zA-Z]{2,})(/[^\s#]*)?", text)
        if bare:
            token = "https://" + bare.group(0)
    if token:
        token = token.rstrip(").,]")
        if "#" in token:
            raw_tag = token.split("#", 1)[1].strip()
            # The fragment is often percent-encoded (e.g. Persian names) — decode it.
            tag = unquote(raw_tag) or None if raw_tag else None
            token = token.split("#", 1)[0]
        parsed = urlparse(token)
        host = parsed.netloc or None
        # The path segment that is NOT the uuid is the proxy path.
        segs = [s for s in parsed.path.split("/") if s]
        path = next((s for s in segs if s.lower() != uuid), None)
    return ParsedLink(uuid=uuid, host=host, path=path, tag=tag)
