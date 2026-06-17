"""Skill discovery adapters (search). Search-only: each adapter turns a query
into hits carrying a `ref` the existing resolve/fetch/gate pipeline understands.
NO install here. Network is SSRF-safe."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from itertools import zip_longest
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
            hits.append(SkillSearchHit(
                name=str(it.get("name") or skill_id.rsplit("/", 1)[-1]),
                ref=f"github:{source}/{skill_id}",
                registry="skills.sh",
                description="",  # skills.sh search returns no description; the detail view fetches it
                signals={"installs": installs} if isinstance(installs, int) else {},
            ))
        return hits


class ClawHubRegistry:
    """ClawHub — GET /api/v1/skills?search=&limit= → hits with a clawhub:<slug>
    ref (fetched via the zip endpoint, not github). A third-party registry whose
    vetting durin does not control → treated as community-trust, so every install
    still passes the §8.C gate. Degrades to [] on any error."""

    name = "clawhub"
    BASE_URL = "https://clawhub.ai/api/v1"

    async def search(self, query: str, *, limit: int) -> list[SkillSearchHit]:
        try:
            async with ssrf_safe_async_client() as client:
                resp = await client.get(f"{self.BASE_URL}/skills",
                                        params={"search": query, "limit": limit}, timeout=15.0)
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001
            return []
        items = data.get("items", data) if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        hits: list[SkillSearchHit] = []
        for it in items[:limit]:
            if not isinstance(it, dict):
                continue
            slug = it.get("slug")
            if not isinstance(slug, str) or not slug:
                continue
            name = it.get("displayName") or it.get("name") or slug
            desc = it.get("summary") or it.get("description") or ""
            hits.append(SkillSearchHit(name=str(name), ref=f"clawhub:{slug}",
                                       registry="clawhub", description=str(desc)))
        return hits


async def search_registries(query, *, adapters, allowlist, limit) -> list[SkillSearchHit]:
    """Query every adapter in parallel; dedupe by ref (first adapter wins),
    round-robin interleave, float allowlisted refs to the front, truncate.
    A slow/failing adapter contributes [] — never sinks the rest."""
    async def _safe(a) -> list[SkillSearchHit]:
        try:
            return await asyncio.wait_for(a.search(query, limit=limit), timeout=15.0)
        except Exception:  # noqa: BLE001
            return []
    per_adapter = await asyncio.gather(*[_safe(a) for a in adapters]) if adapters else []
    seen: set[str] = set()
    lists: list[list[SkillSearchHit]] = []
    for hits in per_adapter:
        deduped = []
        for h in hits:
            if h.ref in seen:
                continue
            seen.add(h.ref)
            deduped.append(h)
        lists.append(deduped)
    merged = [h for group in zip_longest(*lists) for h in group if h is not None]
    pref = [p for p in (allowlist or []) if p]
    allow_refs = {h.ref for h in merged if any(h.ref.startswith(p) for p in pref)}
    ordered = [h for h in merged if h.ref in allow_refs] + \
              [h for h in merged if h.ref not in allow_refs]
    return ordered[:limit]


def build_adapters(registries) -> list:
    """Instantiate enabled adapters from config (a list of SkillRegistryConfig).
    Wires skills.sh + clawhub; unknown kinds are skipped."""
    out = []
    for r in registries:
        if not getattr(r, "enabled", True):
            continue
        if r.kind == "skills.sh":
            out.append(SkillsShRegistry())
        elif r.kind == "clawhub":
            out.append(ClawHubRegistry())
    return out
