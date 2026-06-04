# Skill Memory Class — Implementation Plan (Spec 2, full integration)

> ✅ **EXECUTED — historical execution record.** The feature this plan built is shipped and verified. Current as-built state: [`docs/architecture/skills/00_overview.md`](../../architecture/skills/00_overview.md). The unchecked `- [ ]` boxes below are the original TDD task list, **not pending work**.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.
>
> **Line numbers drift** — every touchpoint here was located by an exhaustive code audit (2026-06-02), but **match by SYMBOL (function/dict name), not by line number**; the audit's own critic found line drift. Verify each anchor against the real file before editing.

**Goal:** Make `skill` a first-class, searchable **memory class** so `memory_search` returns procedural skills (`kind="skill"`) alongside facts, kept in sync on every skill mutation — without touching E1's git-authored skills_store model.

**Architecture (D1 — decided, do not revisit):** `skill` is a **pseudo-class exactly like `entity_page`** — a synthetic indexable kind sourced from a parallel walk root (`workspace/skills/<name>/SKILL.md`), NOT a `MEMORY_CLASSES` member. It reuses the proven `entity_page` machinery (own loader, own `upsert_*`, own URI translation, rebuild pass, store-side keep-in-sync) while respecting that skills are git-versioned and LLM-authored via `skills_store`. **Adding `"skill"` to `MEMORY_CLASSES` is the single most dangerous mistake — never do it.**

**Tech Stack:** Python, pytest (`tmp_path`), lancedb (vector), sqlite FTS5, the durin memory index/search pipeline.

**Spec (source of truth):** [`docs/superpowers/specs/2026-06-02-skills-retrieval-spec2-design.md`](../specs/2026-06-02-skills-retrieval-spec2-design.md).

---

## The URI contract (D2 — decide once, hold across 5 modules)

This is the **highest silent-failure risk**. One shape, enforced everywhere:

- **Vector `id` + FTS `uri`** = `skill/<slug>` (`<slug>` = the skill directory name).
- **On-disk path stored in the row** = `skills/<slug>/SKILL.md`.
- **Search/drill resolve** `skill/<slug>` → `skills/<slug>/SKILL.md`.
- Wherever a hit carries `path` (the stored relative path), **use `hit.path` directly** instead of reconstructing.

Encode this ONCE as helpers in `paths.py` and reuse them in indexer / vector_index / search_pipeline / memory_search / drill. If the shape diverges across modules, RRF double-counts (split scores drop good skills below top-K) and every hit is undrillable — both silent.

---

## Do-NOT-touch list (active restraint — a plausible edit here silently corrupts shipped behavior)

| File:symbol | Why leave it |
|---|---|
| `paths.py` `MEMORY_CLASSES` | Adding `"skill"` mints `memory/skill/`, breaks every `memory/`-rooted walker + the store model |
| `tools/memory_store.py` `_AGENT_FACING_CLASSES` | Adding `"skill"` lets the LLM write SKILL.md into `memory/skill/` — outside the git store, unindexed |
| `archive.py` whitelist `("stable","corpus","session_summary")` | Already excludes skill; routing skills here is wrong (skill "archive" = git history) |
| v2 dream (`dream.py`/`dream_apply.py`/`absorption.py`/`archive.py`/`dream_runner.py`) + `dream_prompt_builder.py` + `templates/dream/*` | No dream path does an all-classes walk (verified); must NOT learn to author/consolidate skills |
| The 8 `MEMORY_CLASSES` walkers (`search.py`, `vector_index.py` rebuild, `aliases_index.py`, `vault_readme.py`, `hot_layer.py` ×2, `cli/tui/startup.py`, `command/builtin.py` ×2) | All `memory/`-rooted; they correctly skip skills by construction |
| `command/builtin.py` `/memory list`/`show` | Skills surface via `/skills`, not `/memory` — intentional exclusion |

---

## Phase 0 — Foundation (no behavior change yet)

### Task 0.1: `SkillPage` loader + path helpers + URI helpers

**Files:**
- Create: `durin/memory/skill_page.py`
- Modify: `durin/memory/paths.py` (`skills_dir`, `skill_dir`, `walk_skills`, `skill_uri`, `skill_path_from_uri`, `__all__`)
- Test: `tests/memory/test_skill_page.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_skill_page.py
from durin.memory.skill_page import SkillPage
from durin.memory.paths import walk_skills, skill_uri, skill_path_from_uri


def _mk(ws, name, desc="does things", body="Step 1\nStep 2\n", mode="auto", disabled=False):
    d = ws / "skills" / name; d.mkdir(parents=True)
    dis = "    disable_model_invocation: true\n" if disabled else ""
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\nmetadata:\n  durin:\n"
        f"    mode: {mode}\n{dis}---\n{body}", encoding="utf-8")


def test_skill_page_parses_frontmatter_and_body(tmp_path):
    _mk(tmp_path, "git-helper", desc="git rebase flow", body="do rebase\n")
    p = tmp_path / "skills" / "git-helper" / "SKILL.md"
    sp = SkillPage.from_file(p)
    assert sp is not None
    assert sp.name == "git-helper"
    assert sp.description == "git rebase flow"
    assert "do rebase" in sp.body
    assert sp.mode == "auto"
    assert sp.disabled is False


def test_skill_page_none_for_missing_or_tombstoned(tmp_path):
    assert SkillPage.from_file(tmp_path / "nope" / "SKILL.md") is None
    _mk(tmp_path, "dead", disabled=True)
    sp = SkillPage.from_file(tmp_path / "skills" / "dead" / "SKILL.md")
    assert sp is not None and sp.disabled is True  # parsed, caller decides to skip


def test_walk_skills_finds_all_and_skips_underscore(tmp_path):
    _mk(tmp_path, "a"); _mk(tmp_path, "b")
    (tmp_path / "skills" / "_scratch").mkdir(parents=True)
    (tmp_path / "skills" / "_scratch" / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")
    found = sorted(p.parent.name for p in walk_skills(tmp_path))
    assert found == ["a", "b"]


def test_uri_helpers_roundtrip(tmp_path):
    assert skill_uri("git-helper") == "skill/git-helper"
    assert skill_path_from_uri("skill/git-helper") == "skills/git-helper/SKILL.md"
```

- [ ] **Step 2: Run → fail** (`pytest tests/memory/test_skill_page.py -v` → ModuleNotFoundError)

- [ ] **Step 3: Implement**

`durin/memory/skill_page.py` — model on `durin/memory/entity_page.py` `EntityPage.from_file`. Reuse `durin.agent.skills_frontmatter.split_frontmatter` (already used by skills_store) so indexed text == LLM-taught text:

```python
"""SkillPage — a parsed view of a skills/<name>/SKILL.md for indexing.

Mirrors entity_page.EntityPage but rooted at workspace/skills/ and authored by
the git-backed skills_store (not the dream consolidator). `from_file` returns
None for missing/unreadable files so rebuild walkers skip silently.
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillPage:
    name: str
    description: str
    body: str
    mode: str
    disabled: bool
    path: Path

    @classmethod
    def from_file(cls, path: Path) -> "SkillPage | None":
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        from durin.agent.skills_frontmatter import split_frontmatter
        data, body = split_frontmatter(text)
        name = data.get("name") or path.parent.name
        durin = (data.get("metadata") or {}).get("durin") if isinstance(data.get("metadata"), dict) else {}
        durin = durin if isinstance(durin, dict) else {}
        disabled = bool(durin.get("disable_model_invocation") or data.get("disable_model_invocation"))
        return cls(
            name=str(name), description=str(data.get("description", "")),
            body=body, mode=str(durin.get("mode", "")), disabled=disabled, path=path,
        )
```

In `durin/memory/paths.py`, after the existing dir helpers, add (and export in `__all__`):

```python
SKILLS_DIRNAME = "skills"

def skills_dir(workspace: Path) -> Path:
    return Path(workspace) / SKILLS_DIRNAME

def skill_dir(workspace: Path, name: str) -> Path:
    return skills_dir(workspace) / name

def walk_skills(workspace: Path):
    """Yield every skills/<name>/SKILL.md, skipping _-prefixed dirs (symmetry with walk_memory)."""
    root = skills_dir(workspace)
    if not root.is_dir():
        return
    for md in sorted(root.rglob("SKILL.md")):
        if any(part.startswith("_") for part in md.relative_to(root).parts):
            continue
        yield md

def skill_uri(slug: str) -> str:
    return f"skill/{slug}"

def skill_path_from_uri(uri: str) -> str:
    slug = uri.split("/", 1)[1] if uri.startswith("skill/") else uri
    return f"{SKILLS_DIRNAME}/{slug}/SKILL.md"
```

- [ ] **Step 4: Run → pass.** Step 5: Commit (`feat(memory): SkillPage loader + skill path/URI helpers`).

### Task 0.2: Config toggle `index_skills`

**Files:** Modify `durin/config/schema.py` (`MemoryConfig`); Test `tests/config/test_memory_config_skills.py`.

- [ ] Add `index_skills: bool = True` to `MemoryConfig` (mirror the existing `enabled` field's style). Test that it defaults True and round-trips. (Per user-memory "user-config over gating", a flat default-on toggle is the right pattern.) Commit.

---

## Phase 1 — Index + lifecycle (skills exist in the indices, kept in sync)

> This phase carries the **highest-risk silent failures**. The drift-detect + drift-repair fixes (Task 1.3) MUST land in the same phase as "start indexing skills" or the first health check wipes them.

### Task 1.1: `upsert_skill` + `_skill_record` + rebuild Pass 3 (vector)

**Files:** Modify `durin/memory/vector_index.py`; Test `tests/memory/test_vector_index_skill.py`.

- [ ] Test: after `upsert_skill(name=, description=, body=, path=)`, a vector search for the description returns a row with `id="skill/<name>"`, `class_name="skill"`, `path="skills/<name>/SKILL.md"`. After `delete_by_id("skill/<name>")`, it's gone. After `rebuild_from_workspace`, skills are present (create a skill on disk first).
- [ ] Implement `upsert_skill(self, *, name, description, body, path, mode="")` mirroring `upsert_entity_page` (compose text = `name + "\n" + description + "\n" + body`, embed, synthesize record `{id: skill_uri(name), class_name:"skill", summary:description, headline:name, body_length:len(body), vector, valid_from:"", entities:[], path:str(path-relative)}`, `_guard_dim_match` + `_atomic_upsert`, **preserve B6 atomicity**). Add `_skill_record(self, skill, md_file, vec)` (same shape). In `rebuild_from_workspace`, add **Pass 3**: `for md in walk_skills(ws): sp=SkillPage.from_file(md); if sp and not sp.disabled: batch...`; update the early-return guard to `and not skills`.
- [ ] Run + commit (`feat(memory): index skills in vector store (upsert_skill + rebuild pass 3)`).

### Task 1.2: FTS `_payload_for_skill` + `reindex_one_skill` (FTS)

**Files:** Modify `durin/memory/indexer.py`; Test `tests/memory/test_indexer_skill.py`.

- [ ] Test: `reindex_one_skill(ws, skill_md)` upserts an FTS row with `uri="skill/<name>"`, `type_="skill"`; an FTS query for the body matches; deleting the SKILL.md then `reindex_one_skill` evicts it; `rebuild_fts_index` includes skills.
- [ ] Implement `_payload_for_skill(ws, skill_md)` (try `relative_to(skills_dir(ws))` → emit `{uri: skill_uri(slug), path: skill_path_from_uri(...), type_:"skill", entity_type:None, text:_skill_text(sp), mtime}`), `_skill_text(sp)` = `name + desc + full body` (FTS indexes full body), `_uri_for` skill-root branch (must equal `_payload_for_skill`'s uri), `reindex_one_skill(ws, skill_md, *, trigger="skill_store")` (handles write AND disappeared-file → `delete_by_uri(skill_uri(slug))`; emit `memory.index.write` trigger="skill_store"), and a second loop in `rebuild_fts_index` after `walk_memory`. Relax `ensure_index_fresh` dir-gate to `... or skills_dir(ws).is_dir()`.
- [ ] Run + commit.

### Task 1.3: Drift detect + repair must SEE skills (the silent-delete fix — HIGH)

**Files:** Modify `durin/memory/indexer.py` (`detect_index_staleness`), `durin/memory/health_check.py` (`_repair_drift_issue`); Test `tests/memory/test_skill_drift.py`.

- [ ] **Step 1: failing test** — index a skill, then run drift detection + repair; assert the skill row **survives** (today it's flagged `row_for_missing_file` and the repair deletes it):

```python
def test_drift_repair_does_not_delete_indexed_skill(tmp_path):
    # build a workspace with one indexed skill, run detect + repair, assert it survives.
    # (construct via the real reindex_one_skill + detect_index_staleness + _repair_drift_issue;
    #  match the real fixtures used by tests/memory/test_*drift*.py)
    ...
```

- [ ] **Step 2: run → fail** (repair deletes the skill).
- [ ] **Step 3: fix BOTH sides:**
  - `detect_index_staleness`: build `fs_files` from `walk_memory` **+ `walk_skills`** (compute skill uris via `skill_uri(slug)` exactly as `_payload_for_skill`).
  - `health_check.py:_repair_drift_issue`: add a branch — `if uri.startswith("skill/"): reindex_one_skill(ws, ws / skill_path_from_uri(uri), trigger="drift_repair")` — BEFORE the bare-id else branch that only scans `("episodic","stable","corpus")`.
- [ ] **Step 4: run → pass.** Step 5: commit (`fix(memory): drift detect+repair recognize indexed skills (no silent delete)`).

### Task 1.4: `index_meta.py` schema bump 4→5

- [ ] Bump `CURRENT_SCHEMA_VERSION` 4→5 so existing workspaces auto-rebuild (and pick up skills) via `ensure_index_fresh`. Test that the rebuild includes skills. Commit. (The bump **forces** a rebuild — Pass 3 / FTS-second-loop MUST be done first, or the upgrade is the data-loss event.)

### Task 1.5: `skills_store` keep-in-sync hook (the core requirement)

**Files:** Modify `durin/agent/skills_store.py`; Test `tests/agent/test_skills_store_index_sync.py`.

- [ ] Test (with a fake/real index): every mutation syncs the index. `dream_create_skill` → upsert; `apply_skill_edit` ok-path → re-index, proposed-path → NO sync; `save_skill_content` → re-index; `set_mode`/`mark_curated` → re-index; `dream_fuse_skills` → upsert(target) + remove(each source). Manual skill never auto-written.
- [ ] Implement `_sync_index(ws, name)` (upsert vector via `upsert_skill` from the skill's `SkillPage` + FTS via `reindex_one_skill`) and `_unsync_index(ws, name)` (vector `delete_by_id(skill_uri(name))` + FTS `delete_by_uri(skill_uri(name))`). **Lazy config read** (`load_config().memory.index_skills`; skip when off) so the module stays pure-over-`Path` for `tmp_path` tests (guard the index calls behind `vector_index_available()` + the toggle; in unit tests with no index, `_sync_index` is a no-op). Wire into: `dream_create_skill`, `apply_skill_edit` (ok-path only), `save_skill_content`, `set_mode`, `mark_curated`, and `dream_fuse_skills` (multi-op: sync target, unsync each removed/tombstoned source). Honor `disable_model_invocation` (tombstoned/fused → `_unsync_index`, not a live hit).
- [ ] Run + commit. **All other entrypoints (web, command, tools, curation, legacy dream) route through these — no separate hooks.**

### Task 1.6: VERIFY LIVE (per feedback_verify_live)

- [ ] Build the binary, `durin memory reindex`, author a skill via `/skills` or the tool, confirm vector+FTS rows exist (`skill/<name>`), run a health check and confirm the skill is **not** deleted, delete the skill and confirm eviction. Document the result. (No commit — verification gate.)

---

## Phase 2 — Search / render (skills retrievable & visible)

### Task 2.1: `Result.kind` + `search_skills` grep-fallback

**Files:** Modify `durin/memory/search.py`; Test `tests/memory/test_search_skill_kind.py`.

- [ ] Test: a `Result(class_name="skill")` has `.kind == "skill"`. `search_skills(ws, needle)` finds a cold skill by description.
- [ ] In `Result.kind`, add `if self.class_name == "skill": return "skill"` **before** the entity_page fallthrough. Add `search_skills(ws, needle, level)` mirroring `_search_entity_pages` (walk `walk_skills`, parse via `split_frontmatter`, emit `Result(source="memory", uri=skill_path_from_uri(skill_uri(slug)), class_name="skill", headline=name, summary=description)`); wire into the dispatcher for grep-fallback coverage. Commit.

### Task 2.2: Search pipeline URI branch (H28 — HIGH)

**Files:** Modify `durin/memory/search_pipeline.py` (`_safe_vector_search`); Test `tests/memory/test_pipeline_skill_uri.py`.

- [ ] Test: a vector hit with `class_name="skill"`, `id="skill/git-helper"` produces `uri="skills/git-helper/SKILL.md"` (NOT `memory/skill/...`), so it fuses (not split-scores) with the FTS hit of the same uri.
- [ ] In `_safe_vector_search`, **before** the generic `memory/<class>/<id>` reconstruction, add `if class_name == "skill": uri = skill_path_from_uri(id)` (or use the row's stored `path`). Assert `_resolve_meta` passes `"skill"` untouched (it does). Commit.

### Task 2.3: Section render — the 3-dict lockstep + marker (KeyError risk)

**Files:** Modify `durin/memory/sectioned_output.py`, `durin/memory/section_markers.py`; Test `tests/memory/test_sectioned_skill.py`.

- [ ] Test: a `SectionedHit(type="skill")` renders under a `=== SKILL: <name> ===` section (not the fragment default), with the skill intro, and rendering does NOT raise.
- [ ] **In lockstep (atomic edit — partial = KeyError that crashes ALL rendering):** `_SECTION_FOR_TYPE["skill"]="skill"`; add `"skill"` to `_SECTION_ORDER` (recommend **first** — procedural playbook leads); `_SECTION_INTRO["skill"]="Procedures matching the query — follow these steps to execute the task; they are instructions, not facts to cite."`; `_marker_for` → skill branch; entities-tail guard → `if section not in ("canonical","skill") and hit.entities`. Add `skill_marker(uri, *, completeness="")` → `=== SKILL: <name> ===` to `section_markers.py` + `__all__`. Commit.

### Task 2.4: `memory_search` tool — type map, result/body resolution, `kinds` filter, description

**Files:** Modify `durin/agent/tools/memory_search.py`, `durin/agent/tools/memory_drill.py`, `durin/memory/drill.py`, `docs/architecture/memory/06_prompts_and_instructions.md`; Test `tests/agent/test_memory_search_skill.py`.

- [ ] Test: `memory_search` returns a `kind="skill"` result whose `uri == "skills/<name>/SKILL.md"` (drillable); `kinds="skill"` returns only skills, `kinds="fact"` excludes them; cold-tier returns the skill body (not empty); `memory_drill("skills/<name>/SKILL.md")` resolves.
- [ ] `_TYPE_FROM_CLASS["skill"]="skill"`; `_sectioned_to_result` skill branch (`class_name="skill"`, `uri = hit.path or hit.uri`, `source="memory"`); `_enrich_body` skill branch (read `ws/skills/<name>/SKILL.md` directly — the `memory/<class>/<id>` split mis-reads a skill uri); add `kinds` param (`["skill","fact","all"]`, default `"all"`) + post-filter at the undreamed-filter site; update the tool description with a `=== SKILL: <name> ===` bullet + "follow, not cite" sentence **and `doc 06 §3.1` in lockstep** (sync test). In `drill.py` add `_translate_skill_uri` (`skill/<slug>` and `skills/<slug>/SKILL.md` → the file) called alongside the entity translation; document in `_URI_DESCRIPTION`. Commit.

### Task 2.5: VERIFY LIVE — `memory_search` returns/render/drills a skill. (gate, no commit)

---

## Phase 3 — Dream guard (confirm, don't change)

### Task 3.1: Regression test — dream never sweeps skills

**Files:** Test `tests/memory/test_dream_ignores_skills.py` (no source change).
- [ ] Test that a `DreamRunner`/consolidation pass over a workspace containing skills does NOT index/consolidate/archive any skill (no `skill/` rows produced by the dream; skills untouched on disk). Asserts the verified-safe invariant. Commit.

---

## Phase 4 — Prompts (teach the LLM the `skill` kind)

### Task 4.1: identity.md + skills_section.md

**Files:** Modify `durin/templates/agent/identity.md`, `durin/templates/agent/skills_section.md`, docs; Test: the existing prompt/template tests + any sync test.
- [ ] In `identity.md`, add a **fifth** memory-kind bullet: *"Skills — procedural memory: step-by-step procedures the agent follows for recurring tasks. A `skill` hit is an instruction set to **execute**, not a fact to cite."* Reword the "four kinds" count. In "Working with search results", add: a `skill` hit is **followed as a procedure, not cited as a fact**. In `skills_section.md`, clarify the two surfaces (always-on catalog via `read_file` vs `memory_search` returning `kind="skill"`). Update `docs/ARCHITECTURE.md` + memory architecture docs (MEMORY.md mandates doc currency). Run template/sync tests + commit.

---

## Phase 5 — Context hot-tier — SHIPPED 2026-06-03 (→ [`2026-06-03-skills-hot-tier.md`](2026-06-03-skills-hot-tier.md))

> Built as a separate plan. The `skills_catalog` block now injects a usage-ranked
> **working set** (top frequent-7d ∪ recent, filled to a config budget) instead of
> the full catalog, memoized per session (prefix-cache safe), gated by
> `memory.skills_hot_tier` (toggle off = full catalog). The long tail is reachable
> via `memory_search`, and a §5.2 prompt nudge tells the agent to search when the
> working set doesn't cover the task. Note vs the original deferral guess: we modify
> the existing `skills_catalog` block (before `memory_hot`) per spec §7 — NOT a new
> block "at/after memory_hot" — because the memoized working set is more
> cache-stable than the per-turn `memory_hot`; `hot_layer.py` is untouched.

---

## Phase 6 — Config wiring

### Task 6.1: Thread `index_skills` through the read sites
- [ ] `skills_store._sync_index` already gates on it (Task 1.5). Also gate `memory_search` skill rendering/filter and the `durin memory reindex` / curation-cron skills pass to skip when `index_skills=False`. Test the off-path is a clean no-op. Commit.

---

## Phase 7 — Telemetry (count skills + skill-miss signal)

### Task 7.1: Recall skill count + miss event + stats

**Files:** Modify `durin/telemetry/schema.py`, `durin/memory/stats.py`, `durin/cli/memory_cmd.py`; Test `tests/telemetry/test_skill_telemetry.py` + the schema sync test.
- [ ] Add `skill_result_count: NotRequired[int]` to `MemoryRecallEvent` (emit from `memory_search`). New `MemorySkillMissEvent` TypedDict (`query, result_count, had_skill_candidate, iteration, session_key`) emitted when a `kinds="skill"` query yields zero (mirror `memory.search.failure`). **Register in BOTH `EVENTS` and `__all__`** (`"memory.skill_miss"`) or the schema sync test flags an orphan. In `stats.py`: `recall_skill_total` + `skill_miss_total` + `_apply_event` increments. `cli/memory_cmd.py`: render "skill recalls"/"skill misses" rows. Commit.

---

## Risk register (verify each is closed before final)

1. **Silent skill deletion via drift repair (HIGHEST)** — Task 1.3 fixes BOTH `detect_index_staleness` AND `health_check._repair_drift_issue`. Must land WITH "start indexing skills".
2. **URI divergence (H28)** — one shape via `paths.skill_uri`/`skill_path_from_uri`, enforced in indexer/vector/pipeline/tool/drill (Tasks 1.1-1.2, 2.2, 2.4).
3. **Skills vanish on rebuild/schema-bump** — vector Pass 3 + FTS second loop (Tasks 1.1-1.2) BEFORE the 4→5 bump (Task 1.4).
4. **Section KeyError crashes ALL rendering** — 3 dicts edited atomically (Task 2.3).
5. **`memory_store` skill write** — hard exclusion (do-not-touch).
6. **Prefix-cache regression** — only if Phase 5 pursued; place at end-of-stable.
7. **Sync-test drift** — tool description ↔ doc 06 §3.1; telemetry `EVENTS` ↔ `__all__` (Tasks 2.4, 7.1).
8. **`dream_fuse_skills` orphan rows** — multi-op unsync of every source (Task 1.5).
9. **Builtin/workspace precedence** — same `skill/<slug>` id, workspace-wins (upsert in place on fork) (Task 1.5 / SkillPage).

---

## Self-review (run before handing off)

- **Spec coverage:** Phase 0-2 = the spec's "skill as memory class + lifecycle foundation + memory_search typed". Phase 4 = the LLM-facing kind. Phase 6 = config gate. Phase 7 = miss telemetry. Phase 5 (hot tier) = the spec's §2.2 — **deferred** per D7 (flag to user).
- **No placeholders:** the load-bearing/novel code (SkillPage, upsert_skill, URI helpers, drift fix, section dicts, _sync_index) is shown in full; mechanical edits give the exact change + test. Implementers match by SYMBOL (lines drift).
- **Type consistency:** the URI shape `skill/<slug>` ↔ `skills/<slug>/SKILL.md` is identical across all tasks (D2). `class_name`/`type_`/`kind`/section name all = `"skill"`.

---

## Execution outcome (2026-06-03)

All phases executed via subagent-driven TDD in worktree `durin-smc` (branch `skills-memory-class`). Phases 0-4, 6, 7 shipped; Phase 5 (hot working-set) deferred per D7. Full suite green (5422 passed, 16 skipped). Verified live end-to-end against a real lance+FTS index (search returns/renders/drills a skill; `kinds` filter; cold-tier body enrichment; dream never sweeps skills).

**Final review found and fixed two defects:**
- **B1 (BLOCKER, fixed `0e47360`):** the three search arms emitted divergent fusion URIs for one skill (FTS `skill/<slug>` vs vector+grep `skills/<slug>/SKILL.md`), so RRF split the score and duplicated the hit. The H28 skill-branch comment falsely claimed FTS wrote the long form. Fix: fusion key is uniformly the canonical stored key `skill/<slug>` (vector `_safe_vector_search` uses `raw_id`; grep `search_skills` uses `skill_uri(slug)`); the drillable `skills/<slug>/SKILL.md` is resolved only at the result boundary via `_skill_uri_to_path(hit.path or hit.uri)`. Regression `test_skill_rrf_fusion.py` drives the real pipeline with both arms and asserts exactly one fused hit.
- **M1 (fixed `2b21687`):** a skill indexed while `index_skills=True` still surfaced after the flag was set `False` (lexical/vector rows linger; drift repair no-ops on `row_for_missing_file`). Fix: `memory_search` drops skill-typed hits when `not skills_indexing_enabled()`, so disabling the flag is an immediate read-side no-op regardless of lingering rows.

**M2 (accepted, not a skill regression):** `ensure_index_fresh` rebuilds only FTS on a schema-version bump, never the vector index — for *all* classes, not just skills (entity pages behave identically). Vector skill rows are created incrementally on each mutation (`_sync_index`) and on a manual `durin memory reindex`. No data loss; consistent with existing system behavior. Left as-is to avoid changing global rebuild semantics.
