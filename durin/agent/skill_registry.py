"""Skill discovery adapters (search). Search-only: each adapter turns a query
into hits carrying a `ref` the existing resolve/fetch/gate pipeline understands.
NO install here. Network is SSRF-safe."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from durin.security.network import ssrf_safe_async_client


@dataclass
class SkillSearchHit:
    name: str
    ref: str                 # github:owner/repo[/dir] | https://…/SKILL.md | clawhub:slug
    registry: str
    description: str = ""
    signals: dict = field(default_factory=dict)   # installs/stars — display + tiebreak only


class SkillRegistry(Protocol):
    name: str
    async def search(self, query: str, *, limit: int) -> list[SkillSearchHit]: ...


class SkillsShRegistry:
    """skills.sh — GET /api/search?q=&limit= → github-backed hits. Degrades to []
    on any error (a registry must never break search)."""

    name = "skills.sh"
    SEARCH_URL = "https://skills.sh/api/search"

    async def search(self, query: str, *, limit: int) -> list[SkillSearchHit]:
        try:
            async with ssrf_safe_async_client() as client:
                resp = await client.get(self.SEARCH_URL,
                                        params={"q": query, "limit": limit}, timeout=15.0)
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            return []
        items = data.get("skills", []) if isinstance(data, dict) else []
        hits: list[SkillSearchHit] = []
        for it in items[:limit]:
            if not isinstance(it, dict):
                continue
            source = str(it.get("source") or "")
            skill_id = str(it.get("skillId") or "")
            if not source or not skill_id:
                continue
            installs = it.get("installs")
            label = f" · {installs:,} installs" if isinstance(installs, int) else ""
            hits.append(SkillSearchHit(
                name=str(it.get("name") or skill_id.rsplit("/", 1)[-1]),
                ref=f"github:{source}/{skill_id}",
                registry="skills.sh",
                description=f"skills.sh: {source}{label}",
                signals={"installs": installs} if isinstance(installs, int) else {},
            ))
        return hits
