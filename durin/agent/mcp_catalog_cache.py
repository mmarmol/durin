"""Local MCP catalog cache + fuzzy ranking.

The official registry's ``search`` is substring-on-name only, which is poor for a
non-technical user who doesn't know exact server names. We sync the (small)
self-published catalog into a local JSON cache via cursor pagination + the
``updated_since`` incremental cursor, then rank fuzzily over name + description
with the stdlib ``difflib`` (no new dependency).
"""
from __future__ import annotations

import json
from difflib import SequenceMatcher
from pathlib import Path

from durin.agent.mcp_registry import McpServerHit, _hit_from_server
from durin.utils.atomic_write import atomic_write_text


def _score(query: str, text: str) -> float:
    """Substring match beats fuzzy; fuzzy uses difflib ratio."""
    if not text:
        return 0.0
    q, t = query.lower(), text.lower()
    if q in t:
        return 1.0 + (len(q) / max(len(t), 1))
    return SequenceMatcher(None, q, t).ratio()


class McpCatalogCache:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._servers: list[dict] = []
        self._meta: dict = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8") or "{}")
                self._servers = raw.get("servers", [])
                self._meta = raw.get("meta", {})
            except (OSError, json.JSONDecodeError):
                self._servers, self._meta = [], {}

    async def sync(self, registry) -> int:
        """Pull every page from ``registry`` (cursor pagination) into the cache."""
        by_name = {s.get("name"): s for s in self._servers if s.get("name")}
        cursor = None
        updated_since = self._meta.get("updated_since")
        while True:
            servers, cursor = await registry.fetch_page(
                cursor=cursor, updated_since=updated_since
            )
            for s in servers:
                if s.get("name"):
                    by_name[s["name"]] = s
            if not cursor:
                break
        self._servers = list(by_name.values())
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            self._path,
            json.dumps({"servers": self._servers, "meta": self._meta}, ensure_ascii=False),
        )
        return len(self._servers)

    def rank(self, query: str, *, limit: int) -> list[McpServerHit]:
        scored: list[tuple[float, dict]] = []
        for s in self._servers:
            sc = max(_score(query, s.get("name", "")), _score(query, s.get("description", "")))
            if sc > 0.2:
                scored.append((sc, s))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [_hit_from_server(s, registry="official") for _, s in scored[:limit]]
