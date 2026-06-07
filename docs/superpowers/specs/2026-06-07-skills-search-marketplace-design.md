# Skills registry search: marketplace-style results

Date: 2026-06-07
Status: approved (design), pending implementation plan
Surface: webui Skills "Add" (acquire) pane + one additive backend endpoint

## Problem

The "Add a skill" pane pairs a registry search with a manual import box in a way
that feels purposeless: the search results don't say what each skill *does*,
there's no result count, no sorting, and no way to page through more. Looking at
established marketplaces (VS Code, npm) the missing pieces are: a result count, an
objective sort control, a per-result description, a source badge, and "load more".

### Hard data constraint (verified this session)

The primary registry **skills.sh returns only `{id, skillId, name, installs,
source}`** — no description, tags, rating, or date (confirmed against its
`/api/search` API; there is no public per-skill detail endpoint — 404). The
description shown today (`skills.sh: <source> · N installs`) is **synthetic**.
clawhub *does* return a real summary/description. So a real description for a
skills.sh hit must be fetched on demand from the skill's `SKILL.md` frontmatter.

## Decisions (from brainstorming)

- **Descriptions:** lazy-fetch on expand. A result is collapsed by default; expanding
  fetches the real `SKILL.md` frontmatter `description` from the hit's ref. clawhub
  hits show their description immediately (no fetch).
- **Structure:** search-first. The registry search is primary (marketplace-style);
  manual "import by reference" (path / URL / github: / clawhub:) becomes a secondary,
  collapsed affordance.

## Isolation guarantee (per user caution)

The registry search is **shared** with non-webui consumers:

- `durin/agent/tools/skill_search.py` — the agent-facing `skill_search` tool.
- `durin/agent/skill_acquire.py`, `durin/agent/tools/skill_acquire_seed.py` — the
  dream skill-acquire flow.
- All of the above go through `durin/agent/skill_registry.py`
  (`search_registries`, `SkillSearchHit`, the skills.sh / clawhub adapters).

**This work MUST NOT modify** `skill_registry.py` (`search_registries`,
`SkillSearchHit`, adapters) or `web_skill_search`. It is purely additive: a new
read-only `describe` endpoint plus a frontend rewrite. Sorting and "load more" are
client-side / use the existing `limit` param. No shared signature changes.

## Relevant current code

- `durin/agent/skills_store.py` — `web_skill_search(workspace, query, limit)` (UNCHANGED).
- `durin/agent/skill_registry.py` — `SkillSearchHit{name, ref, registry, description,
  signals{installs}}`, `search_registries`, adapters (UNCHANGED, shared).
- `durin/agent/skills_import.py` — `_parse_github_ref`, `_GITHUB_RAW`, `_http_get_bytes`,
  `_gh_headers`; `durin/agent/skills_frontmatter.py:split_frontmatter` (reused by describe).
- `durin/config/schema.py` — `skills.discovery.search_limit = 10`.
- `webui/src/components/SkillsView.tsx` — acquire pane (search + import forms, hit list).
- `webui/src/lib/api.ts` — `searchSkills(token, query, limit)`, `SkillSearchHit`.

## Design

### 1. Backend — lazy describe endpoint (additive)

`GET /api/skills/describe?ref=<ref>` → `{ ref, description }`.

`web_skill_describe(ref)` in `skills_store.py`:
- `github:owner/repo[/dir]` → resolve to the raw `SKILL.md` URL via the existing
  `_parse_github_ref` + `_GITHUB_RAW`, fetch with `_http_get_bytes` (size-bounded,
  e.g. ≤ 64 KB), `split_frontmatter`, return the `description` field truncated to
  ~280 chars. Read-only; never executes anything; never writes to disk.
- `clawhub:` → return `{description: ""}` (the UI already has the hit's description;
  it won't call describe for clawhub hits).
- Any error (network, no frontmatter, missing field) → `{description: ""}`. A failed
  describe is non-fatal; the UI shows "no description available".
- New websocket route `GET /api/skills/describe` in `websocket.py`, mirroring the
  existing skill routes (token-checked, off-thread via `asyncio.to_thread`).

### 2. Frontend — search-first acquire pane

Rewrite the acquire branch of `SkillsView.tsx`:

- **Header:** "Add a skill" + one explainer line ("public-registry skills; importing
  drops them into Pending for your approval").
- **Primary search:** the search input + button (as today, promoted to the top).
- **Results bar:** "N results" + a **sort** control — `Installs ↓` (default), `Name A–Z`,
  `Relevance` (the registry order as returned). Sorting is client-side over loaded hits.
- **Result card:** name + source badge (skills.sh / clawhub) + installs stat (when
  present) + an expand chevron + Import `[+]`. Expanding:
  - clawhub hit with a description → show it directly.
  - otherwise → call `describeSkill(ref)`, cache the result per ref in component state,
    show the description, a "no description available" line on empty, or a small spinner
    while loading.
- **Load more:** when the returned hit count equals the current limit (more may exist),
  show "Show more" which re-queries `searchSkills` with `limit += 10` and replaces the list.
- **Secondary:** a collapsed "Import by reference" disclosure containing the existing
  manual import form (path / URL / github: / clawhub:), default collapsed.

### 3. API + i18n

- `api.ts`: add `describeSkill(token, ref): Promise<{ ref: string; description: string }>`.
  `SkillSearchHit` unchanged.
- i18n (es/en): result count, sort labels (installs / name / relevance), expand/collapse,
  "no description available", "Show more", "Import by reference", the explainer line.

## Key states

- Search: idle, loading (spinner on the button), empty ("nothing matched"), results.
- Result row: collapsed, expanded-loading, expanded-with-description, expanded-empty.
- Load more: idle, loading, exhausted (hidden when fewer than `limit` returned).
- Import by reference: collapsed (default) / expanded; reuses the existing import → gate flow.

## Error handling

- `describe` failure → `{description: ""}`; UI shows "no description available" (never blocks).
- search failure → existing readable error path (`errMsg`).
- A github raw fetch is bounded and best-effort; GitHub rate-limit / 404 degrade to empty.

## Testing

Backend:
- `web_skill_describe`: github ref → parses frontmatter `description`; missing frontmatter
  / network error → `""`; clawhub ref → `""` (passthrough); oversized body is bounded.
- A guard test asserting `skill_registry.search_registries` / `SkillSearchHit` /
  `web_skill_search` are unchanged in signature (import + call shape) — the isolation
  contract with the agent `skill_search` tool.

Frontend (vitest):
- results render with a count and a source badge; changing sort reorders the list
  (installs vs name);
- expanding a skills.sh hit calls `describeSkill` and renders the returned description;
  a clawhub hit shows its description without calling describe;
- "Show more" re-queries with a larger limit;
- the collapsed "Import by reference" still drives `importSource`.

## Rollout

Frontend + one additive backend endpoint; no migration, no shared-search change. CI runs
pytest only (webui not built in CI); webui verified locally (vitest + build + live).
The new `describe` endpoint takes effect once the gateway is redeployed (frozen-wheel
deploy step); the frontend degrades gracefully against an older gateway (describe 404 →
"no description available").
