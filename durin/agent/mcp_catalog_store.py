"""MCP server catalog — vendored floor + optional downloaded overlay.

Floor: ``durin/agent/data/mcp_catalog.json`` (committed, always present).
Overlay: ``<data_dir>/mcp_catalog_cache.json`` (written by the refresh task).

``load_servers()`` returns the overlay's servers when the overlay exists AND
its ``generated_at`` >= the floor's; otherwise returns the floor's servers.
Corrupt/missing overlay or floor degrades gracefully — never raises.
"""

from __future__ import annotations

import json
from difflib import SequenceMatcher
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


def _score(query: str, text: str) -> float:
    """Substring match beats fuzzy; fuzzy uses difflib ratio."""
    if not text:
        return 0.0
    q, t = query.lower(), text.lower()
    if q in t:
        return 1.0 + (len(q) / max(len(t), 1))
    return SequenceMatcher(None, q, t).ratio()


_SIGNAL_KEYS = (
    "stars", "owner_login", "owner_url", "owner_avatar",
    "topics", "language", "license", "official", "repo_url",
)


def search(
    query: str,
    *,
    limit: int,
    quality: str = "official",
    min_stars: int = 100,
) -> list:
    """Search the local catalog store.

    Returns a list of McpServerHit sorted by (stars desc, score desc).
    """
    from durin.agent.mcp_registry import McpServerHit

    servers = load_servers()
    gated = quality != "all"

    scored: list[tuple[float, int, dict]] = []
    for s in servers:
        sc = max(_score(query, s.get("name", "")),
                 _score(query, s.get("description", "")))
        if sc <= 0.2:
            continue
        stars = s.get("stars") or 0
        official = bool(s.get("official"))
        if gated and not (stars > min_stars or official):
            continue
        scored.append((sc, stars if stars else -1, s))

    scored.sort(key=lambda t: (t[1], t[0]), reverse=True)

    hits = []
    for _, _, s in scored[:limit]:
        hits.append(McpServerHit(
            name=s["name"],
            ref=s.get("ref") or s["name"],
            registry="official",
            kind=s.get("kind", "local"),
            description=s.get("description", ""),
            signals={k: s[k] for k in _SIGNAL_KEYS if k in s},
        ))
    return hits
