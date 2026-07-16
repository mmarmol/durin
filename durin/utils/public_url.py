"""Resolution of durin's public base URL.

Exactly two consumers by design: the MCP OAuth redirect (where the provider
sends the user's browser back) and the dashboard URL surfaced by ``durin
status`` / ``durin gateway``. Vendor-owned OAuth flows (codex, openrouter)
must NOT use this — their redirect URIs are fixed by the vendor's app.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


def _normalize(url: str) -> str | None:
    """Return ``scheme://netloc`` for a bare http(s) origin, else None."""
    try:
        p = urlparse(url.strip())
    except Exception:  # noqa: BLE001
        return None
    if p.scheme not in ("http", "https") or not p.netloc:
        return None
    if p.path.strip("/") or p.query or p.fragment or p.params:
        return None
    return f"{p.scheme}://{p.netloc}"


def resolve_public_base_url(config: Any) -> str | None:
    """The operator-declared public base URL, normalized; None when unset/invalid."""
    raw = getattr(getattr(config, "gateway", None), "public_url", None)
    return _normalize(raw) if raw else None


def validate_origin(origin: str) -> str | None:
    """A browser-supplied origin, normalized; None unless a bare http(s) origin."""
    return _normalize(origin) if origin else None


def dashboard_url(config: Any) -> str:
    """Where the dashboard is reached: public_url, else the websocket host:port."""
    public = resolve_public_base_url(config)
    if public:
        return public
    ws = getattr(getattr(config, "channels", None), "websocket", None)
    host, port = "127.0.0.1", 8765
    if ws is not None:
        if isinstance(ws, dict):
            host = ws.get("host", host) or host
            port = ws.get("port", port) or port
        else:
            host = getattr(ws, "host", host) or host
            port = getattr(ws, "port", port) or port
    return f"http://{host}:{port}"
