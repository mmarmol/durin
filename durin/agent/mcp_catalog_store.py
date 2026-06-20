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


def _name_segment(ref_or_name: str) -> str:
    """Return the part after the last '/' — the server-name segment."""
    return ref_or_name.rsplit("/", 1)[-1].lower()


# Capability synonyms: a query on the left also matches servers mentioning any term on
# the right. Vendor/official servers describe themselves by brand, not capability — e.g.
# Atlassian's server is literally "Atlassian Rovo MCP Server" and never says "jira" — so
# a capability query would miss it without this. Kept tiny and brand-grounded on purpose.
_ALIASES = {
    "jira": ("atlassian",),
    "confluence": ("atlassian",),
    "bitbucket": ("atlassian",),
}


def _matches(query_lc: str, s: dict) -> bool:
    """Return True when query_lc is a meaningful match for server *s*.

    A server matches when query_lc (or one of its capability aliases) is a SUBSTRING of:
    - the name segment (part of ref/name after the last '/')
    - owner_login
    - description
    - any topics entry

    Typo fallback: SequenceMatcher ratio > 0.8 vs the name segment only.
    Loose whole-string / namespace fuzzy matching is intentionally excluded
    to prevent "nada" matching long descriptions or "github" matching every
    io.github.* namespace ref.
    """
    name_seg = _name_segment(s.get("ref") or s.get("name", ""))
    desc = (s.get("description") or "").lower()
    owner = (s.get("owner_login") or "").lower()
    topics = [t.lower() for t in (s.get("topics") or [])]

    terms = (query_lc, *_ALIASES.get(query_lc, ()))
    for term in terms:
        if term in name_seg or term in owner or term in desc:
            return True
        if any(term in topic for topic in topics):
            return True
    # Typo fallback — only against the short name segment, only for the raw query
    if SequenceMatcher(None, query_lc, name_seg).ratio() > 0.8:
        return True
    return False


_SIGNAL_KEYS = (
    "stars", "owner_login", "owner_url", "owner_avatar",
    "topics", "language", "license", "official", "verified", "repo_url",
)


def _matched_ranked(query_lc: str) -> list[dict]:
    """Servers matching the query, ranked: curated (verified) first — each tier by stars.

    Two tiers only (the model is "curated + popular"): ``verified`` (GitHub-curated) sorts
    above everything, and WITHIN each tier the order is stars-desc. ``official`` (the
    namespace heuristic) is deliberately NOT a ranking signal — it stays only as a display
    flag in signals.
    """
    matched = [s for s in load_servers() if _matches(query_lc, s)]
    matched.sort(
        key=lambda s: (bool(s.get("verified")), s.get("stars") or 0),
        reverse=True,
    )
    return matched


def _curated_or_popular(s: dict, min_stars: int) -> bool:
    """The default gate: a server is shown when it is curated (verified) OR popular
    (stars over the floor). The star floor is the toggleable part — quality='all' /
    ``search_tiered``'s ``more`` list bypass it."""
    return bool(s.get("verified")) or (s.get("stars") or 0) > min_stars


def _to_hit(s: dict):
    from durin.agent.mcp_registry import McpServerHit

    return McpServerHit(
        name=s["name"],
        ref=s.get("ref") or s["name"],
        registry="github" if s.get("verified") else "official",
        kind=s.get("kind", "local"),
        description=s.get("description", ""),
        signals={k: s[k] for k in _SIGNAL_KEYS if k in s},
    )


def search(
    query: str,
    *,
    limit: int,
    quality: str = "official",
    min_stars: int = 100,
) -> list:
    """Search the local catalog store → McpServerHit list, ranked verified-then-stars.

    Default (``quality != "all"``) gates to curated-or-popular; ``quality="all"`` returns
    every match (the explicit "show everything" path).
    """
    ranked = _matched_ranked(query.lower())
    if quality != "all":
        ranked = [s for s in ranked if _curated_or_popular(s, min_stars)]
    return [_to_hit(s) for s in ranked[:limit]]


def search_tiered(
    query: str,
    *,
    limit: int,
    min_stars: int = 100,
) -> tuple[list, list]:
    """Return ``(hits, more)`` for progressive disclosure.

    ``hits`` = curated + popular (the default view); ``more`` = the matches below the star
    floor (the "+N less popular" reveal). Both are ranked verified-then-stars and capped at
    ``limit``. This is the webui's single-call source — no "show all" mode, no second fetch.
    """
    ranked = _matched_ranked(query.lower())
    gated = [s for s in ranked if _curated_or_popular(s, min_stars)]
    more = [s for s in ranked if not _curated_or_popular(s, min_stars)]
    return [_to_hit(s) for s in gated[:limit]], [_to_hit(s) for s in more[:limit]]
