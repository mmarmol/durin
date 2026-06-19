"""MCP server catalog — vendored floor + optional downloaded overlay.

Floor: ``durin/agent/data/mcp_catalog.json`` (committed, always present).
Overlay: ``<data_dir>/mcp_catalog_cache.json`` (written by the refresh task).

``load_servers()`` returns the overlay's servers when the overlay exists AND
its ``generated_at`` >= the floor's; otherwise returns the floor's servers.
Corrupt/missing overlay or floor degrades gracefully — never raises.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_FLOOR = Path(__file__).parent / "data" / "mcp_catalog.json"


def _overlay_path() -> Path | None:
    try:
        from durin.config.paths import get_data_dir

        return get_data_dir() / "mcp_catalog_cache.json"
    except Exception:  # noqa: BLE001
        return None


def _read_catalog(path: Path) -> dict | None:
    """Return parsed catalog dict or None on any error."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and isinstance(raw.get("servers"), list):
            return raw
    except Exception:  # noqa: BLE001
        pass
    return None


@lru_cache(maxsize=1)
def _cached_load() -> list[dict]:
    floor = _read_catalog(_FLOOR)
    floor_servers: list[dict] = floor["servers"] if floor else []
    floor_ts: str = (floor or {}).get("generated_at", "")

    overlay_file = _overlay_path()
    if overlay_file is not None and overlay_file.exists():
        overlay = _read_catalog(overlay_file)
        if overlay is not None:
            overlay_ts: str = overlay.get("generated_at", "")
            if overlay_ts >= floor_ts:
                return overlay["servers"]

    return floor_servers


def load_servers() -> list[dict]:
    """Return the active MCP server catalog as a list of server dicts."""
    return _cached_load()


def cache_clear() -> None:
    """Invalidate the in-process cache so the next call re-reads from disk."""
    _cached_load.cache_clear()
