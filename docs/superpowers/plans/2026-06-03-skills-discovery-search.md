# Skills discovery — search backend (increment 1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Give durin a unified skill search over pluggable registries (skills.sh first), exposed as a `skill_search` agent tool. Each hit carries a `ref` that flows into the existing `skill_import` gate — search NEVER installs.

**Architecture:** A thin search-only adapter layer (durin already has resolve/fetch/gate). `SkillRegistry.search(query) -> [SkillSearchHit]`; an async orchestrator queries enabled adapters in parallel, dedupes by `ref`, round-robin interleaves, floats allowlisted refs, truncates. Config lives at `skills.discovery` (from the §9 reorg). Spec: [docs/superpowers/specs/2026-06-03-skill-discovery-registries-design.md](docs/superpowers/specs/2026-06-03-skill-discovery-registries-design.md) §2.

**Tech Stack:** Python, pydantic, httpx via `durin.security.network.ssrf_safe_async_client`, pytest.

**Out of scope of *this* increment — but ALL built in later increments this session:**
web search box ✅, `durin skill search` CLI ✅, clawhub adapter ✅ (zip fetch branch),
`skill_update` → reframed and built as drift→evolution (§8.D) ✅, the unverified-origin
sweep ✅ (Part C).

> **STATUS: EXECUTED (2026-06-03).** Increment 1 (this plan) plus every "later
> increment" above shipped + live-verified against the real APIs (skills.sh +
> clawhub) and the real curation judge (the drift merge). Full suite green.

**Branch safety:** shared checkout — before EVERY commit verify `git branch --show-current` == `skills-hot-tier`, else STOP. No Claude attribution.

---

### Task 1: Config — `skills.discovery` (registries + search_limit)

**Files:**
- Modify: `durin/config/schema.py` (add `SkillRegistryConfig`, `SkillsDiscoveryConfig`; add `discovery` field to `SkillsConfig`)
- Test: `tests/config/test_skills_discovery_config.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/config/test_skills_discovery_config.py
from durin.config.schema import Config


def test_discovery_defaults():
    d = Config().skills.discovery
    assert d.search_limit == 10
    # ships with skills.sh enabled by default
    assert [r.kind for r in d.registries] == ["skills.sh"]
    assert d.registries[0].enabled is True


def test_registry_camel_roundtrip():
    c = Config.model_validate({"skills": {"discovery": {"registries": [
        {"name": "clawhub", "kind": "clawhub", "enabled": False, "apiKeySecret": "ch"}]}}})
    r = c.skills.discovery.registries[0]
    assert r.kind == "clawhub" and r.enabled is False and r.api_key_secret == "ch"
```

- [ ] **Step 2: Run → FAIL** (`Config().skills` has no `discovery`)

Run: `.venv/bin/python -m pytest tests/config/test_skills_discovery_config.py -q`

- [ ] **Step 3: Implement** — in `schema.py`, just above `class SkillsConfig(Base)`:

```python
class SkillRegistryConfig(Base):
    """One search registry. ``kind`` selects the adapter; ``api_key_secret`` names
    a durin secret (empty → anonymous). ``taps`` is github-only (repos to search)."""

    name: str
    kind: Literal["skills.sh", "clawhub", "github", "well-known"]
    enabled: bool = True
    api_key_secret: str = ""
    taps: list[str] = Field(default_factory=list)


class SkillsDiscoveryConfig(Base):
    """Skill discovery: which registries to search + how many results."""

    registries: list[SkillRegistryConfig] = Field(
        default_factory=lambda: [SkillRegistryConfig(name="skills.sh", kind="skills.sh")])
    search_limit: int = 10
```

Then add to `class SkillsConfig(Base)` (keep `security`):

```python
    discovery: SkillsDiscoveryConfig = Field(default_factory=SkillsDiscoveryConfig)
```

- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** (`feat(config): add skills.discovery registries config`)

---

### Task 2: Adapter layer — `SkillSearchHit`, `SkillRegistry`, skills.sh adapter

**Files:**
- Create: `durin/agent/skill_registry.py`
- Test: `tests/agent/test_skill_registry_skillssh.py` (new)

- [ ] **Step 1: Failing test** (mock the SSRF client so no network)

```python
# tests/agent/test_skill_registry_skillssh.py
import pytest
from durin.agent.skill_registry import SkillsShRegistry, SkillSearchHit


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload
    def json(self): return self._p
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(self.status_code)


class _Client:
    def __init__(self, resp): self._resp = resp
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False
    async def get(self, url, **kw): return self._resp


@pytest.mark.asyncio
async def test_skillssh_maps_items_to_github_refs(monkeypatch):
    payload = {"skills": [
        {"id": "openai/skills/pdf", "source": "openai/skills", "skillId": "pdf",
         "name": "pdf", "installs": 1200}]}
    monkeypatch.setattr("durin.agent.skill_registry.ssrf_safe_async_client",
                        lambda: _Client(_Resp(200, payload)))
    hits = await SkillsShRegistry().search("pdf", limit=5)
    assert hits == [SkillSearchHit(
        name="pdf", ref="github:openai/skills/pdf", registry="skills.sh",
        description=hits[0].description, signals={"installs": 1200})]
    assert hits[0].ref.startswith("github:")


@pytest.mark.asyncio
async def test_skillssh_non_200_returns_empty(monkeypatch):
    monkeypatch.setattr("durin.agent.skill_registry.ssrf_safe_async_client",
                        lambda: _Client(_Resp(500, {})))
    assert await SkillsShRegistry().search("x", limit=5) == []
```

- [ ] **Step 2: Run → FAIL** (module missing)

- [ ] **Step 3: Implement** `durin/agent/skill_registry.py`:

```python
"""Skill discovery adapters (§6.C search). Search-only: each adapter turns a
query into hits carrying a `ref` the existing resolve/fetch/gate pipeline
understands. NO install here. Network is SSRF-safe."""
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
    """skills.sh — GET /api/search?q=&limit= → github-backed hits. Endpoint
    reverse-engineered from the public CLI; degrades to [] on any error."""

    name = "skills.sh"
    SEARCH_URL = "https://skills.sh/api/search"

    async def search(self, query: str, *, limit: int) -> list[SkillSearchHit]:
        try:
            async with ssrf_safe_async_client() as client:
                resp = await client.get(self.SEARCH_URL,
                                        params={"q": query, "limit": limit}, timeout=15.0)
                resp.raise_for_status()
                data = resp.json()
        except Exception:  # noqa: BLE001 — a registry never breaks search
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
```

- [ ] **Step 4: Run → PASS** (`.venv/bin/python -m pytest tests/agent/test_skill_registry_skillssh.py -q`)
- [ ] **Step 5: Commit** (`feat(skills): SkillSearchHit + skills.sh search adapter`)

---

### Task 3: Orchestrator — `search_registries` (round-robin merge)

**Files:**
- Modify: `durin/agent/skill_registry.py` (add the orchestrator + a config→adapters builder)
- Test: `tests/agent/test_skill_registry_orchestrator.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/agent/test_skill_registry_orchestrator.py
import pytest
from durin.agent.skill_registry import SkillSearchHit, search_registries


class _Fake:
    def __init__(self, name, hits, *, boom=False):
        self.name, self._hits, self._boom = name, hits, boom
    async def search(self, query, *, limit):
        if self._boom:
            raise RuntimeError("down")
        return self._hits[:limit]


def _h(ref, reg):
    return SkillSearchHit(name=ref.split("/")[-1], ref=ref, registry=reg)


@pytest.mark.asyncio
async def test_dedupe_by_ref_and_round_robin():
    a = _Fake("a", [_h("github:o/r/x", "a"), _h("github:o/r/y", "a")])
    b = _Fake("b", [_h("github:o/r/x", "b"), _h("github:o/r/z", "b")])  # x dup
    out = await search_registries("q", adapters=[a, b], allowlist=[], limit=10)
    refs = [h.ref for h in out]
    assert refs == ["github:o/r/x", "github:o/r/z", "github:o/r/y"]  # round-robin, x deduped


@pytest.mark.asyncio
async def test_failing_adapter_does_not_sink_others():
    a = _Fake("a", [], boom=True)
    b = _Fake("b", [_h("github:o/r/z", "b")])
    out = await search_registries("q", adapters=[a, b], allowlist=[], limit=10)
    assert [h.ref for h in out] == ["github:o/r/z"]


@pytest.mark.asyncio
async def test_allowlisted_floats_to_front():
    a = _Fake("a", [_h("github:other/r/x", "a"), _h("github:acme/r/y", "a")])
    out = await search_registries("q", adapters=[a], allowlist=["github:acme/"], limit=10)
    assert out[0].ref == "github:acme/r/y"
```

- [ ] **Step 2: Run → FAIL** (`search_registries` missing)

- [ ] **Step 3: Implement** — append to `skill_registry.py`:

```python
import asyncio
from itertools import zip_longest


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
    """Instantiate enabled adapters from config (SkillRegistryConfig list).
    Unknown/clawhub/github/well-known kinds are skipped for now (skills.sh only
    in this increment)."""
    out = []
    for r in registries:
        if not getattr(r, "enabled", True):
            continue
        if r.kind == "skills.sh":
            out.append(SkillsShRegistry())
    return out
```

- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Commit** (`feat(skills): round-robin cross-registry search orchestrator`)

---

### Task 4: `skill_search` agent tool

**Files:**
- Create: `durin/agent/tools/skill_search.py` (mirror `durin/agent/tools/skill_import.py` structure + registration)
- Test: `tests/agent/test_skill_search_tool.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/agent/test_skill_search_tool.py
import pytest
from types import SimpleNamespace
from durin.agent.tools.skill_search import SkillSearchTool
from durin.agent.skill_registry import SkillSearchHit


@pytest.mark.asyncio
async def test_skill_search_returns_hits(monkeypatch):
    async def fake_search(query, *, adapters, allowlist, limit):
        return [SkillSearchHit(name="pdf", ref="github:o/r/pdf", registry="skills.sh",
                               description="d", signals={"installs": 9})]
    monkeypatch.setattr("durin.agent.tools.skill_search.search_registries", fake_search)
    tool = SkillSearchTool(workspace="/tmp", registries=[SimpleNamespace(kind="skills.sh", enabled=True)],
                           allowlist=[])
    out = await tool.execute(query="pdf")
    assert out["hits"][0]["ref"] == "github:o/r/pdf"
    assert out["hits"][0]["registry"] == "skills.sh"
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement** `durin/agent/tools/skill_search.py` — mirror `skill_import.py`: a `Tool` subclass with `tool_parameters_schema(query=…, limit=…)`, `name="skill_search"`, `read_only=True`, `create(cls, ctx)` reading `ctx.app_config.skills.discovery.registries` + `skills.security.allowlist` (fall back to `load_config()`), and:

```python
    async def execute(self, **kwargs):
        from durin.agent.skill_registry import build_adapters, search_registries
        query = str(kwargs.get("query", "")).strip()
        if not query:
            return {"error": "query is required"}
        limit = int(kwargs.get("limit", self._limit) or self._limit)
        hits = await search_registries(query, adapters=build_adapters(self._registries),
                                       allowlist=self._allowlist, limit=limit)
        return {"hits": [{"name": h.name, "ref": h.ref, "registry": h.registry,
                          "description": h.description, "signals": h.signals} for h in hits],
                "note": "to import a hit: skill_import(action='fetch', source=<ref>)"}
```

Register it in the same place `SkillImportTool`/`SkillAuditTool` are auto-discovered into the core toolset (follow the existing pattern — grep for `SkillImportTool` registration).

- [ ] **Step 4: Run → PASS**
- [ ] **Step 5: Verify the tool description tells the agent to pipe hits through the gate** (read `skill_search.py`'s description string — it must point at `skill_import`, never install directly).
- [ ] **Step 6: Commit** (`feat(skills): skill_search agent tool over the registry orchestrator`)

---

### Task 5: Integration green

- [ ] Run `.venv/bin/python -m pytest tests/config/ tests/agent/ -q` → all green.
- [ ] Confirm the agent tool is discoverable: grep that `skill_search` is registered alongside `skill_import`.
- [ ] Full backend suite `.venv/bin/python -m pytest -q` → no regressions.
