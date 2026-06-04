# Skills Hot Working-Set Tier Implementation Plan

> ✅ **EXECUTED — historical execution record.** The feature this plan built is shipped and verified. Current as-built state: [`docs/architecture/skills/00_overview.md`](../../architecture/skills/00_overview.md). The unchecked `- [ ]` boxes below are the original TDD task list, **not pending work**.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the always-injected *full* skills catalog in the cache-stable prefix with a usage-ranked **working set** (most-used skills), so context cost stays bounded as the catalog grows while the long tail stays reachable via `memory_search` (already shipped).

**Architecture:** A pure helper ranks skill names from the durable `skill_calls` signal (frequent-over-7d ∪ recent), filled to a config budget from the catalog in stable order. `ContextBuilder` computes this set **once per session** (memoized → prefix-cache safe) and restricts the existing `skills_catalog` block to it. A config toggle defaults the hot tier on but allows falling back to the full catalog for A/B calibration. No new search tool, no change to `hot_layer.py`.

**Tech Stack:** Python, pydantic config (`durin/config/schema.py`), pytest. Reuses `collect_recent_skill_calls` (already aggregates `{skill:{op:count}}` from session `.meta.json` sidecars with a `within_hours` window) and `SkillsLoader.build_skills_summary`.

**Spec (source of truth, design already decided 2026-06-02):** [`docs/superpowers/specs/2026-06-02-skills-retrieval-spec2-design.md`](../specs/2026-06-02-skills-retrieval-spec2-design.md) §2.2, §3, §7 (change map), §8 (decisions). This plan is the deferred "Phase 5 — Context hot-tier" of [`2026-06-02-skill-memory-class.md`](2026-06-02-skill-memory-class.md).

**What already shipped (PR #22) vs. what's in scope here — verified 2026-06-03 against the merged code:**
- ✅ **Miss telemetry (§4):** `memory.skill_miss` emits when a `kinds="skill"` search yields zero (Task 7.1). In scope here: nothing.
- ⚠️ **Prompt nudge (§5.2): NOT shipped as specified.** `skills_section.md` only says skills are *searchable*; it does **not** carry the load-bearing nudge *"if nothing in the hot tier covers the task, search (`memory_search` kind=skill) before proceeding or concluding no skill exists."* Worse, its current line **"This catalog is always available"** becomes **false** once the hot tier injects only the working set (not the full catalog). Phase 5 removes the long tail from context, so this nudge + reframe become load-bearing **here** → **Task 5**.
- ✅ **skill_calls signal wiring:** produced in `loop.py:1809` → persisted to `derived.skill_calls` via `manager.py:_DERIVED_METADATA_KEYS` → read by `collect_recent_skill_calls`. The working set has real data (the 2h dream already consumes it). In scope here: only the new ranking helper.

**Test command** (work happens in the main checkout `/Users/marcelo/git_personal/durin`, on branch `skills-hot-tier` — the editable install resolves to this tree):
```
cd /Users/marcelo/git_personal/durin && /Users/marcelo/git_personal/durin/.venv/bin/python -m pytest <paths> -v
```

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `durin/config/schema.py` | New `SkillsHotTierConfig` nested under `MemoryConfig.skills_hot_tier` (enabled + sizes + windows) | 1 |
| `durin/agent/skill_usage.py` | New pure `compute_working_set(workspace, candidates, *, recent, frequent, …)` — usage-rank + fill-to-budget | 2 |
| `durin/agent/skills.py` | `build_skills_summary` gains an `include` filter (restrict to a name set) | 3 |
| `durin/agent/context.py` | `ContextBuilder` computes the working set once (memoized, config-driven, toggle) and restricts the `skills_catalog` block to it | 4 |
| `durin/templates/agent/skills_section.md` (+ `identity.md`) | reframe "always available catalog" → working-set + long-tail-via-search, and add the §5.2 search nudge (load-bearing once the long tail leaves context) | 5 |
| (verify) | Live end-to-end against a real workspace with `skill_calls` sidecars | 6 |
| docs | Mark Phase 5 shipped in the skill-memory-class plan + roadmap | 7 |

**Decisions locked from spec §8:** sizes ~15 recent / ~30 frequent (favor frequent), config-driven, generous-because-cached, calibrate later with the already-shipped miss telemetry. Hot tier shows **name + short description** (the current `build_skills_summary` line format `- **name** — desc \`path\`` already is exactly this — the `path` is the `read_file` handle). We only **restrict which** skills appear; we do not change the line format or `skills_active` (always-on full bodies).

**Prefix-cache invariant (critical):** the working set is computed **once per `ContextBuilder` instance** (one instance per session, `loop.py:325`) and memoized, so the stable layer stays byte-identical across turns of a session. A new session = new instance = freshly-ranked set. This mirrors how `memory_hot` rotates daily (stable within its window). Do NOT recompute per turn.

**Placement — reconciliation with the deferred note (drift resolved 2026-06-03).** The skill-memory-class plan's deferred Phase 5 note said *"block at/after `memory_hot`"*. We deliberately do **not** follow that literally: spec §7 (source of truth) says modify the **existing** `skills_catalog` block, which sits at position 4 of the stable layer (`identity → bootstrap → skills_active → skills_catalog → memory_hot`), i.e. **before** `memory_hot`. The cache analysis confirms this is correct, not just compatible: the working set is **memoized** (byte-identical for the whole session) while `memory_hot` is **re-read every turn** (`read_hot_layer(...).render()` is not memoized and can rotate under dream mid-session). Cache invalidation propagates forward from the first changed byte, so the **more-stable** block must come **first** — keeping the memoized working set ahead of the per-turn `memory_hot` means a mid-session hot-layer rotation never invalidates the working set's cached position. The note's "at/after memory_hot" assumed a *new* block and is the less-informed guess; we keep the existing position. We still honor the note's hard constraint: **never before `identity`/`bootstrap`/`skills_active`**.

**Breakdown key (minor, decided).** The note suggested registering a *new* `stable_labels` breakdown key. We reuse the existing `skills_catalog` key (it already tracks this exact block, which now holds the working set) — no telemetry-key churn, no test churn. The human-facing label stays "Skills catalog"; it is acceptably accurate (it is the injected skills block) and not worth a churn.

---

## Task 1: Config — `SkillsHotTierConfig`

**Files:**
- Modify: `durin/config/schema.py` (add a `Base` subclass near the other `Memory*` configs; add one field to `MemoryConfig`)
- Test: `tests/config/test_skills_hot_tier_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_skills_hot_tier_config.py
from durin.config.schema import Config


def test_defaults():
    cfg = Config()
    ht = cfg.memory.skills_hot_tier
    assert ht.enabled is True
    assert ht.recent == 15
    assert ht.frequent == 30
    assert ht.frequent_window_hours == 168.0
    assert ht.recent_window_hours == 24.0


def test_camel_alias_roundtrip():
    # global alias_generator=to_camel → nested key is skillsHotTier with camel fields
    cfg = Config.model_validate(
        {"memory": {"skillsHotTier": {"enabled": False, "recent": 5, "frequent": 8}}}
    )
    ht = cfg.memory.skills_hot_tier
    assert ht.enabled is False and ht.recent == 5 and ht.frequent == 8
    # untouched fields keep defaults
    assert ht.frequent_window_hours == 168.0
```

- [ ] **Step 2: Run it — FAIL** (`AttributeError: ... has no attribute 'skills_hot_tier'`)

Run: `cd /Users/marcelo/git_personal/durin && /Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/config/test_skills_hot_tier_config.py -v`

- [ ] **Step 3: Implement.** Find the base model class used by the other configs (it is `Base` — same class `MemoryEmbeddingConfig(Base)` etc. use; it carries the global `alias_generator=to_camel` + `populate_by_name`). Add the nested config class immediately before `class MemoryConfig` (or with the other `Memory*` configs), then one field on `MemoryConfig`.

```python
class SkillsHotTierConfig(Base):
    """Hot working-set tier for skills (Spec 2 §2.2/§8).

    The cache-stable prefix injects only the usage-ranked working set
    instead of the whole catalog; the long tail is reachable via
    ``memory_search`` (kind="skill"). ``enabled=False`` restores the
    full-catalog injection (A/B / calibration fallback).

    Sizes favor frequent-over-the-window (the durable working set);
    ``recent`` is a smaller recency bonus. Generous because cached —
    calibrate with the shipped ``memory.skill_miss`` telemetry.
    """

    enabled: bool = True
    recent: int = 15
    frequent: int = 30
    frequent_window_hours: float = 168.0  # 7 days
    recent_window_hours: float = 24.0
```

Add to `MemoryConfig` (next to `index_skills`):

```python
    skills_hot_tier: SkillsHotTierConfig = Field(default_factory=SkillsHotTierConfig)
```

- [ ] **Step 4: Run it — PASS.**

- [ ] **Step 5: Commit**

```bash
cd /Users/marcelo/git_personal/durin
git add durin/config/schema.py tests/config/test_skills_hot_tier_config.py
git commit -m "feat(config): MemoryConfig.skills_hot_tier (working-set sizes + windows + toggle)"
```

---

## Task 2: `compute_working_set` helper (pure, usage-ranked + fill)

**Files:**
- Modify: `durin/agent/skill_usage.py` (add one function; it already imports nothing heavy and has `collect_recent_skill_calls`)
- Test: `tests/agent/test_skill_working_set.py`

**Contract:** `compute_working_set(workspace, candidates, *, recent, frequent, frequent_window_hours=168.0, recent_window_hours=24.0) -> list[str]`.
- `candidates` = the eligible skill names (non-`always`, in the catalog's stable display order). Usage signal for names not in `candidates` is ignored (a deleted/renamed skill leaves stale `skill_calls`).
- Rank: top `frequent` candidates by total call-count over `frequent_window_hours`, then top `recent` by count over `recent_window_hours`, deduped (frequent wins ties of placement).
- **Fill to budget:** `budget = max(0,recent)+max(0,frequent)`. If fewer than `min(budget, len(candidates))` names were selected by usage, fill the remaining slots from `candidates` in their given order (skipping already-selected). This makes a small/cold catalog inject *everything* (no regression vs today) and a large cold catalog inject the first `budget` — never an empty hot tier.
- Return at most `budget` names. Order: usage-ranked first, then fill order.

- [ ] **Step 1: Write the failing test.** `collect_recent_skill_calls` reads real session sidecars; the test seeds them via the real writer path is heavy, so monkeypatch the aggregator (it is the documented seam — pure function takes `workspace` only to pass through).

```python
# tests/agent/test_skill_working_set.py
import durin.agent.skill_usage as su
from durin.agent.skill_usage import compute_working_set


def _patch_calls(monkeypatch, by_window):
    """by_window: {window_hours: {skill: total_count}} → fake collect_recent_skill_calls."""
    def fake(workspace, within_hours=None):
        counts = by_window.get(within_hours, {})
        return {s: {"read": c} for s, c in counts.items()}
    monkeypatch.setattr(su, "collect_recent_skill_calls", fake)


def test_frequent_ranked_then_recent_dedup(monkeypatch, tmp_path):
    _patch_calls(monkeypatch, {
        168.0: {"deploy": 9, "rebase": 5, "lint": 1},   # frequent-7d
        24.0: {"hotfix": 3, "deploy": 2},               # recent-24h
    })
    cands = ["deploy", "rebase", "lint", "hotfix", "docs"]
    ws = compute_working_set(tmp_path, cands, recent=2, frequent=2,
                             frequent_window_hours=168.0, recent_window_hours=24.0)
    # frequent top-2 = deploy, rebase ; recent top-2 = hotfix, deploy(dup) → +hotfix
    # budget = 4 → fill one more from candidates order skipping selected → lint
    assert ws == ["deploy", "rebase", "hotfix", "lint"]


def test_small_catalog_injects_everything(monkeypatch, tmp_path):
    _patch_calls(monkeypatch, {168.0: {}, 24.0: {}})   # zero usage (cold)
    cands = ["a", "b", "c"]
    ws = compute_working_set(tmp_path, cands, recent=15, frequent=30)
    assert ws == ["a", "b", "c"]   # filled in candidate order, no empty hot tier


def test_usage_for_unknown_skill_ignored(monkeypatch, tmp_path):
    _patch_calls(monkeypatch, {168.0: {"ghost": 99}, 24.0: {}})
    cands = ["a", "b"]
    ws = compute_working_set(tmp_path, cands, recent=1, frequent=1)
    assert "ghost" not in ws and ws == ["a", "b"]


def test_budget_caps_large_cold_catalog(monkeypatch, tmp_path):
    _patch_calls(monkeypatch, {168.0: {}, 24.0: {}})
    cands = [f"s{i}" for i in range(50)]
    ws = compute_working_set(tmp_path, cands, recent=5, frequent=10)
    assert ws == cands[:15]   # budget = 15, fill in order
```

- [ ] **Step 2: Run it — FAIL** (`ImportError: cannot import name 'compute_working_set'`).

- [ ] **Step 3: Implement** in `durin/agent/skill_usage.py`:

```python
def compute_working_set(
    workspace,
    candidates: list[str],
    *,
    recent: int,
    frequent: int,
    frequent_window_hours: float = 168.0,
    recent_window_hours: float = 24.0,
) -> list[str]:
    """Usage-ranked working set of skill names for the hot tier.

    Top ``frequent`` candidates by call-count over ``frequent_window_hours``
    (the durable working set), then top ``recent`` over ``recent_window_hours``,
    deduped. Filled to ``frequent + recent`` from ``candidates`` (stable order)
    so a small/cold catalog still injects something. Usage for names not in
    ``candidates`` is ignored. Returns at most ``frequent + recent`` names.
    """
    cand_set = set(candidates)

    def _ranked(window: float, top: int) -> list[str]:
        if top <= 0:
            return []
        agg = collect_recent_skill_calls(workspace, within_hours=window)
        totals = {
            s: sum(ops.values())
            for s, ops in agg.items()
            if s in cand_set
        }
        ordered = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
        return [s for s, _ in ordered[:top]]

    out: list[str] = []
    seen: set[str] = set()
    for name in (*_ranked(frequent_window_hours, frequent),
                 *_ranked(recent_window_hours, recent)):
        if name not in seen:
            seen.add(name)
            out.append(name)

    budget = max(0, recent) + max(0, frequent)
    for name in candidates:               # fill in stable catalog order
        if len(out) >= budget:
            break
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out[:budget]
```

- [ ] **Step 4: Run it — PASS** (4 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/marcelo/git_personal/durin
git add durin/agent/skill_usage.py tests/agent/test_skill_working_set.py
git commit -m "feat(skills): compute_working_set — usage-ranked hot set + fill-to-budget"
```

---

## Task 3: `build_skills_summary` gains an `include` filter

**Files:**
- Modify: `durin/agent/skills.py` (`build_skills_summary`, currently at ~line 111 — match by symbol)
- Test: `tests/agent/test_skills_loader.py` (add a test; reuse its existing skill-dir fixture pattern)

**Change:** add `include: set[str] | None = None`. When not None, skip any skill whose name is **not** in `include` (in addition to the existing `exclude` + disable filters). `exclude` still wins (a name in both is skipped). Order and line format are unchanged.

- [ ] **Step 1: Write the failing test.** First read `tests/agent/test_skills_loader.py` to reuse how it builds a workspace with skill dirs (frontmatter `name`/`description`). Then:

```python
def test_build_skills_summary_include_restricts(tmp_path):
    # build a loader over a workspace with skills a, b, c (reuse the file's helper)
    loader = _loader_with_skills(tmp_path, {
        "alpha": "do alpha", "beta": "do beta", "gamma": "do gamma",
    })  # adapt to the real fixture helper in this test file
    out = loader.build_skills_summary(include={"alpha", "gamma"})
    assert "alpha" in out and "gamma" in out
    assert "beta" not in out


def test_include_none_is_full_catalog(tmp_path):
    loader = _loader_with_skills(tmp_path, {"alpha": "x", "beta": "y"})
    out = loader.build_skills_summary()      # include=None → unchanged behavior
    assert "alpha" in out and "beta" in out


def test_exclude_wins_over_include(tmp_path):
    loader = _loader_with_skills(tmp_path, {"alpha": "x", "beta": "y"})
    out = loader.build_skills_summary(exclude={"alpha"}, include={"alpha", "beta"})
    assert "alpha" not in out and "beta" in out
```

- [ ] **Step 2: Run it — FAIL** (`TypeError: build_skills_summary() got an unexpected keyword argument 'include'`).

- [ ] **Step 3: Implement.** Change the signature and add one guard inside the loop. Current head:

```python
    def build_skills_summary(self, exclude: set[str] | None = None) -> str:
```
becomes
```python
    def build_skills_summary(
        self,
        exclude: set[str] | None = None,
        include: set[str] | None = None,
    ) -> str:
```
and right after the existing `if exclude and skill_name in exclude: continue` line, add:
```python
            if include is not None and skill_name not in include:
                continue
```
(Update the docstring `Args:` to mention `include`: "when provided, only these names appear — used by the hot working-set tier.")

- [ ] **Step 4: Run it — PASS.** Regression: `... -m pytest tests/agent/test_skills_loader.py -q`.

- [ ] **Step 5: Commit**

```bash
cd /Users/marcelo/git_personal/durin
git add durin/agent/skills.py tests/agent/test_skills_loader.py
git commit -m "feat(skills): build_skills_summary include= filter (hot working-set)"
```

---

## Task 4: Wire the working set into `ContextBuilder` (memoized, toggled)

**Files:**
- Modify: `durin/agent/context.py` — `ContextBuilder.__init__` (add a memo slot) and `_build_stable_layer` (the `skills_catalog` block, ~lines 182-194 — match by symbol)
- Test: `tests/agent/test_context_hot_tier.py`

**Behavior:**
- Compute the working set **once** (memoized on the instance) → stable layer byte-identical across turns.
- Config via best-effort `load_config()` (same pattern as `skills_store`/`index_meta`; tolerates missing config in unit tests by falling back to defaults). When `memory.skills_hot_tier.enabled` is False → `include=None` (full catalog, today's behavior).
- `candidates` = catalog names minus `always` (the disable filter stays inside `build_skills_summary`).

- [ ] **Step 1: Write the failing test.** Build a workspace with several skills + fake usage; assert (a) only the working set is injected when enabled, (b) the long-tail skill is absent from the block but the prompt still builds, (c) the block is identical across two `build_system_prompt` calls (cache stability), (d) `enabled=False` injects the full catalog.

```python
# tests/agent/test_context_hot_tier.py
import durin.agent.context as ctxmod
from durin.agent.context import ContextBuilder


def _force_hot(monkeypatch, *, enabled=True, recent=1, frequent=1):
    from durin.config.schema import Config
    cfg = Config()
    cfg.memory.skills_hot_tier.enabled = enabled
    cfg.memory.skills_hot_tier.recent = recent
    cfg.memory.skills_hot_tier.frequent = frequent
    monkeypatch.setattr("durin.config.loader.load_config", lambda *a, **k: cfg)


def _seed_skills(ws, names):
    for n in names:
        d = ws / "skills" / n
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {n}\ndescription: do {n}\n---\n# {n}\n")


def test_enabled_injects_only_working_set(tmp_path, monkeypatch):
    _seed_skills(tmp_path, ["deploy", "rebase", "obscure"])
    _force_hot(monkeypatch, enabled=True, recent=1, frequent=1)  # budget 2
    # fake usage: deploy frequent, rebase recent → working set {deploy, rebase}
    monkeypatch.setattr(ctxmod, "compute_working_set",
                        lambda *a, **k: ["deploy", "rebase"])
    cb = ContextBuilder(tmp_path)
    prompt = cb.build_system_prompt()
    block = cb._last_layer_breakdown["stable"].get("skills_catalog", "")
    assert "deploy" in block and "rebase" in block
    assert "obscure" not in block            # long tail not injected


def test_stable_across_turns(tmp_path, monkeypatch):
    _seed_skills(tmp_path, ["deploy", "rebase", "obscure"])
    _force_hot(monkeypatch, enabled=True, recent=1, frequent=1)
    monkeypatch.setattr(ctxmod, "compute_working_set",
                        lambda *a, **k: ["deploy", "rebase"])
    cb = ContextBuilder(tmp_path)
    a = cb.build_system_prompt()
    b = cb.build_system_prompt()
    assert a == b                            # prefix-cache invariant


def test_disabled_injects_full_catalog(tmp_path, monkeypatch):
    _seed_skills(tmp_path, ["deploy", "rebase", "obscure"])
    _force_hot(monkeypatch, enabled=False)
    cb = ContextBuilder(tmp_path)
    cb.build_system_prompt()
    block = cb._last_layer_breakdown["stable"].get("skills_catalog", "")
    assert "obscure" in block                # full catalog when off
```

- [ ] **Step 2: Run it — FAIL** (`compute_working_set` not imported in context.py / long tail still present).

- [ ] **Step 3: Implement.**

In `context.py` imports add:
```python
from durin.agent.skill_usage import compute_working_set
```

In `ContextBuilder.__init__` add a memo slot (next to `self.last_composition`):
```python
        # Hot working-set tier: computed once per instance (= per session)
        # so the stable prefix stays byte-identical across turns.
        self._skill_working_set: set[str] | None = None
        self._skill_working_set_done = False
```

Add a private helper on the class:
```python
    def _hot_tier_include(self, always_skills: list[str]) -> set[str] | None:
        """The working-set name filter for the skills_catalog block, or None
        (full catalog) when the hot tier is disabled. Memoized per instance."""
        if self._skill_working_set_done:
            return self._skill_working_set
        self._skill_working_set_done = True
        try:
            from durin.config.loader import load_config
            ht = load_config().memory.skills_hot_tier
        except Exception:  # noqa: BLE001 — unit tests without a config file
            from durin.config.schema import SkillsHotTierConfig
            ht = SkillsHotTierConfig()
        if not ht.enabled:
            self._skill_working_set = None
            return None
        always = set(always_skills)
        candidates = [
            e["name"] for e in self.skills.list_skills(filter_unavailable=False)
            if e["name"] not in always
        ]
        names = compute_working_set(
            self.workspace, candidates,
            recent=ht.recent, frequent=ht.frequent,
            frequent_window_hours=ht.frequent_window_hours,
            recent_window_hours=ht.recent_window_hours,
        )
        self._skill_working_set = set(names)
        return self._skill_working_set
```

In `_build_stable_layer`, change the `skills_catalog` block to pass `include`:
```python
        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                block = f"# Active Skills\n\n{always_content}"
                breakdown["skills_active"] = block
                parts.append(block)

        include = self._hot_tier_include(always_skills)
        skills_summary = self.skills.build_skills_summary(
            exclude=set(always_skills), include=include,
        )
        if skills_summary:
            block = render_template("agent/skills_section.md", skills_summary=skills_summary)
            breakdown["skills_catalog"] = block
            parts.append(block)
```

- [ ] **Step 4: Run it — PASS** (3 tests). Regression — the cache + composition tests must still pass:
```
cd /Users/marcelo/git_personal/durin && /Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_context_hot_tier.py tests/agent/test_context_prompt_cache.py tests/agent/test_context_builder.py tests/agent/test_context_three_tier_prompt.py tests/agent/test_context_composition_event.py -q
```

- [ ] **Step 5: Commit**

```bash
cd /Users/marcelo/git_personal/durin
git add durin/agent/context.py tests/agent/test_context_hot_tier.py
git commit -m "feat(context): inject usage-ranked skills working set (hot tier), memoized per session"
```

---

## Task 5: Prompt — working-set reframe + §5.2 search nudge

**Files:**
- Modify: `durin/templates/agent/skills_section.md` (the line that today says "This catalog is always available …")
- Modify: `durin/templates/agent/identity.md` (the "Working with search results" area — add one bullet)
- Test: `tests/memory/test_identity_skill_kind.py` (extend; this is the existing sync test that pins skill prompt wording — keep its current asserts green)

**Why (load-bearing under Phase 5):** once Task 4 lands, only the working set is injected — the long tail is no longer in context. The agent must be told to search when the shown skills don't cover the task, otherwise it will conclude "no skill exists" for a skill it simply can't see. The current `skills_section.md` line "This catalog is always available" is now false (only the working set is shown). This is the spec §5.2 nudge, finally load-bearing.

- [ ] **Step 1: Write the failing test.** First read `tests/memory/test_identity_skill_kind.py` (its `_norm` helper + `_SKILLS_SECTION` path). Add:

```python
def test_skills_section_reframed_as_working_set_with_search_nudge():
    t = _norm(_SKILLS_SECTION).lower()
    # the stale "always available [full catalog]" claim is gone
    assert "always available" not in t
    # the §5.2 nudge: search when the shown skills don't cover the task
    assert "memory_search" in t
    assert ("if nothing" in t or "if none" in t or "don't cover" in t
            or "doesn't cover" in t)
    assert ("before" in t and ("proceed" in t or "conclud" in t or "say" in t))
```
Keep the file's existing `test_skills_section_names_both_surfaces` (it asserts `memory_search` + `read` are present — the reframe below preserves both).

- [ ] **Step 2: Run it — FAIL** (`assert "always available" not in t`).

- [ ] **Step 3: Implement.** Rewrite the body line of `durin/templates/agent/skills_section.md` (keep the `# Skills` heading, the read-file sentence, the unavailable-deps sentence, and the `{{ skills_summary }}` placeholder exactly). Replace the "This catalog is always available …" sentence with:

```
The skills above are your **most-used working set**, not the whole catalog. Skills are searchable memory: if nothing above covers the task, search (`memory_search` with `kind="skill"`) **before** proceeding or concluding that no skill exists. It returns matching procedures as `kind="skill"` hits (rendered under `=== SKILL: <name> ===`) — follow them as steps, don't cite them as facts.
```

In `durin/templates/agent/identity.md`, in the "## Working with search results" list (where the "Follow skills, don't cite them" bullet already lives, added in PR #22), add one bullet right after it:

```
- **Search for skills you don't see.** The skills listed in your context are a *working set*, not the full catalog. If none fits the task, call `memory_search` (`kind="skill"`) before deciding no procedure exists.
```

- [ ] **Step 4: Run it — PASS.** Regression (the sync test must stay green):
```
cd /Users/marcelo/git_personal/durin && /Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/memory/test_identity_skill_kind.py tests/memory/test_identity_memory_section.py -q
```

- [ ] **Step 5: Commit**

```bash
cd /Users/marcelo/git_personal/durin
git add durin/templates/agent/skills_section.md durin/templates/agent/identity.md tests/memory/test_identity_skill_kind.py
git commit -m "feat(prompts): reframe skills block as working set + add search-the-long-tail nudge (§5.2)"
```

---

## Task 6: VERIFY LIVE (gate, no commit)

**Goal:** prove the hot tier works end-to-end against a real workspace with real `skill_calls` sidecars and the real `ContextBuilder` — not just monkeypatched units.

- [ ] **Step 1:** Write `/tmp/verify_hot_tier.py` that: (a) creates a workspace with ~5 skills on disk; (b) writes session `.meta.json` sidecars whose `derived.skill_calls` make 2 skills frequent (use `durin/session/session_meta.py` writer — inspect it; or write the sidecar JSON directly with the `derived.skill_calls` shape `[{"skill": "<name>", "op": "read"}, …]` that `collect_recent_skill_calls` reads); (c) sets `memory.skills_hot_tier` small (recent=1, frequent=1) via a real config or monkeypatched `load_config`; (d) builds the system prompt via `ContextBuilder(ws).build_system_prompt()`; asserts the 2 hot skills appear in the `skills_catalog` breakdown and a never-used skill does NOT; (e) builds twice and asserts the stable layer is byte-identical; (f) flips `enabled=False` (new `ContextBuilder`) and asserts the full catalog returns; (g) asserts the rendered prompt contains the §5.2 search nudge (Task 5) so the agent is told to search the long tail it can no longer see.

Run from the checkout (the branch is checked out here, so `durin` resolves to this tree):
```
cd /Users/marcelo/git_personal/durin && /Users/marcelo/git_personal/durin/.venv/bin/python /tmp/verify_hot_tier.py
```
Expected: `HOT TIER LIVE: ALL PASS`.

- [ ] **Step 2:** If it fails, fix the implementation (not the check) and re-run. Do not proceed until green.

---

## Task 7: Docs — mark Phase 5 shipped

**Files:**
- Modify: `docs/superpowers/plans/2026-06-02-skill-memory-class.md` (the "Phase 5 — DEFERRED" block → shipped, link here)
- Modify: `docs/roadmap.md` (the Horizon-2 "Refinamientos shipped" line: note the hot working-set tier shipped, removing "Pendiente fast-follow: hot working-set tier (Phase 5, diferido)")

- [ ] **Step 1:** In `2026-06-02-skill-memory-class.md`, change the Phase 5 heading from `DEFERRED` to `SHIPPED (2026-06-03 → docs/superpowers/plans/2026-06-03-skills-hot-tier.md)` and keep the one-line rationale.
- [ ] **Step 2:** In `docs/roadmap.md`, update the shipped-refinements bullet to state the hot working-set tier shipped (usage-ranked working set in `ContextBuilder`, long tail via `memory_search`), and drop the "Pendiente fast-follow" clause. Bump the "Last updated" line to 2026-06-03.
- [ ] **Step 3: Commit**

```bash
cd /Users/marcelo/git_personal/durin
git add docs/superpowers/plans/2026-06-02-skill-memory-class.md docs/roadmap.md
git commit -m "docs: skills hot working-set tier shipped (Phase 5)"
```

---

## Self-Review

**Spec coverage:** §2.2/§3 hot tier (always + N recent + X frequent, name+desc, body on-demand) → Tasks 2+4. §7 change map row `skill_usage.py` working-set helper → Task 2; row `context.py` catalog→hot-tier → Task 4. §8.1 sizes (~15/~30, favor frequent, config-driven) → Tasks 1+2. §8.3 granularity (hot=name+desc; corpus richer) → unchanged line format (Task 4 note). §8.4 "single system, small catalog covered" → fill-to-budget (Task 2). **§5.2 nudge → Task 5** (verified NOT shipped in PR #22; load-bearing here because Phase 5 removes the long tail from context). §4 miss telemetry already shipped (PR #22) — out of scope.

**Drift cross-check (vs existing specs + plans, 2026-06-03):** (1) deferred-note "block at/after memory_hot" vs spec §7 "modify existing skills_catalog block" → reconciled in the prefix-cache section (we follow the spec; cache analysis favors it). (2) deferred-note "new breakdown key" → reuse `skills_catalog` (documented). (3) deferred-note "watch `_FRAGMENT_CLASSES` in `hot_layer.py`" → not applicable: the working set is the `skills_catalog` block, `hot_layer.py`/`memory_hot` is untouched. (4) sizes/§7 map/skill_calls wiring → verified consistent. (5) the only real gap (the §5.2 nudge) is now Task 5.

**Placeholder scan:** none — every code step shows full code; the only "adapt to the real fixture" is Task 3 Step 1 (reuse the existing `test_skills_loader.py` helper) and Task 5 (inspect `session_meta.py` writer), both explicit about what to read.

**Type consistency:** `compute_working_set(workspace, candidates, *, recent, frequent, frequent_window_hours, recent_window_hours) -> list[str]` defined in Task 2, called identically in Task 4. `build_skills_summary(exclude, include)` defined in Task 3, called with `include=` in Task 4. Config path `memory.skills_hot_tier.{enabled,recent,frequent,frequent_window_hours,recent_window_hours}` consistent across Tasks 1/4. Breakdown key stays `skills_catalog` (no telemetry-key churn; it now holds the working set).

**Out of scope / do-not-touch:** `hot_layer.py` and `_FRAGMENT_CLASSES` (the working set is the separate `skills_catalog` block, not `memory_hot`); `skills_active` (always-on full bodies) unchanged; no new search tool.
