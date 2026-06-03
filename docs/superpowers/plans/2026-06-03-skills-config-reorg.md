# Skills config namespace reorg — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move misplaced skill config out of `memory.*` into a coherent home: a new top-level `skills.security` (← `memory.skill_import`) and `agents.defaults.skills_hot_tier` (← `memory.skills_hot_tier`), with back-compat so existing configs keep working. `memory.index_skills` and `agents.defaults.disabled_skills` stay put.

**Architecture:** Pure config refactor. Pydantic schema change in `durin/config/schema.py` + a legacy-key migration in `durin/config/loader.py:_migrate_config` (same place the `my`-tool migration lives) + mechanical consumer updates. No behavior change. This is **Task 0** of the skill-discovery spec ([docs/superpowers/specs/2026-06-03-skill-discovery-registries-design.md](docs/superpowers/specs/2026-06-03-skill-discovery-registries-design.md) §9); the discovery feature later adds `skills.discovery`.

**Tech Stack:** Python, pydantic / pydantic-settings, pytest; webui TypeScript (config-key strings).

**Branch safety:** shared checkout — before EVERY commit verify `git branch --show-current` == `skills-hot-tier`, else STOP. No Claude attribution in commits.

> **STATUS: EXECUTED (2026-06-03).** Shipped — schema reorg + back-compat migration
> + consumer updates + webui keys. During execution the split-layout writer was found
> to silently drop the new `skills` section on save (a data-loss bug); root-caused
> (derive the section set from the serialized config instead of a hardcoded list) and
> fixed, which also recovered the previously-dropped `telemetry`/`appearance`. Full
> suite green.

---

### Task 1: Schema — new `skills.security`, move `skills_hot_tier` to agent defaults

**Files:**
- Modify: `durin/config/schema.py` (rename `SkillImportConfig`→`SkillSecurityConfig`; add `SkillsConfig`; move `SkillsHotTierConfig` field onto `AgentDefaults`; drop both fields from `MemoryConfig`; add `skills` to root `Config`)
- Test: `tests/config/test_skills_namespace.py` (new)

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_skills_namespace.py
from durin.config.schema import Config


def test_skills_namespace_defaults():
    c = Config()
    # governance moved out of memory.skill_import
    assert c.skills.security.allowlist == []
    assert c.skills.security.llm_judge.trigger == "off"
    assert c.skills.security.max_files == 100
    # per-agent skill-context tuning moved onto agent defaults
    assert c.agents.defaults.skills_hot_tier.frequent == 30
    # memory keeps the index toggle; loses the relocated blocks
    assert c.memory.index_skills is True
    assert not hasattr(c.memory, "skill_import")
    assert not hasattr(c.memory, "skills_hot_tier")
```

- [ ] **Step 2: Run it — expect FAIL** (`AttributeError: 'MemoryConfig' object has no attribute ... ` / `Config` has no `skills`)

Run: `.venv/bin/python -m pytest tests/config/test_skills_namespace.py -q`

- [ ] **Step 3: Implement the schema change**

In `durin/config/schema.py`:
- Rename `class SkillImportConfig(Base)` → `class SkillSecurityConfig(Base)` (fields unchanged: allowlist, github_token_secret, max_files, max_total_bytes, max_file_bytes, install_specs_policy, llm_judge).
- Add right after it:

```python
class SkillsConfig(Base):
    """Global skill-subsystem governance (spec 2026-06-03 §9). Per-agent
    skill-context tuning (skills_hot_tier, disabled_skills) lives on
    `agents.defaults`; the memory-index toggle stays at `memory.index_skills`.
    `discovery` (registries + search) is added by the discovery feature."""

    security: SkillSecurityConfig = Field(default_factory=SkillSecurityConfig)
```

- In `class AgentDefaults` (the class holding `disabled_skills`, `max_messages`), add:

```python
    skills_hot_tier: SkillsHotTierConfig = Field(
        default_factory=SkillsHotTierConfig,
        validation_alias=AliasChoices("skillsHotTier", "skills_hot_tier"),
    )
```

- In `class MemoryConfig`, DELETE the `skills_hot_tier` and `skill_import` fields (keep `index_skills`, `skills_hot_tier`'s class definition stays — only the field moves).
- In root `class Config(BaseSettings)`, add alongside `memory`:

```python
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
```

- `SkillsHotTierConfig` is defined above `MemoryConfig`; `AgentDefaults` is defined below it. If a forward-reference ordering issue arises, move `SkillsHotTierConfig`'s class def above `AgentDefaults` (it has no deps on MemoryConfig).

- [ ] **Step 4: Run the test — expect PASS**

Run: `.venv/bin/python -m pytest tests/config/test_skills_namespace.py -q`

- [ ] **Step 5: Commit**

```bash
test "$(git -C /Users/marcelo/git_personal/durin branch --show-current)" = "skills-hot-tier" || { echo "BRANCH CHANGED"; exit 1; }
git -C /Users/marcelo/git_personal/durin add durin/config/schema.py tests/config/test_skills_namespace.py
git -C /Users/marcelo/git_personal/durin commit -m "refactor(config): move skill_import→skills.security, skills_hot_tier→agents.defaults"
```

---

### Task 2: Back-compat — migrate legacy `memory.skillImport` / `memory.skillsHotTier`

**Files:**
- Modify: `durin/config/loader.py` (`_migrate_config`)
- Test: `tests/config/test_config_migration.py` (add cases)

- [ ] **Step 1: Write the failing tests** (mirror `test_load_config_migrates_legacy_my_tool_keys`)

```python
# append to tests/config/test_config_migration.py
def test_load_config_migrates_legacy_skill_import(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(
        {"memory": {"skillImport": {"allowlist": ["github:acme/"], "maxFiles": 50}}}),
        encoding="utf-8")
    config = load_config(config_path)
    assert config.skills.security.allowlist == ["github:acme/"]
    assert config.skills.security.max_files == 50
    assert not hasattr(config.memory, "skill_import")


def test_load_config_migrates_legacy_skills_hot_tier(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(
        {"memory": {"skillsHotTier": {"frequent": 12}}}), encoding="utf-8")
    config = load_config(config_path)
    assert config.agents.defaults.skills_hot_tier.frequent == 12


def test_save_config_rewrites_legacy_skill_keys(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(
        {"memory": {"skillImport": {"allowlist": ["github:acme/"]},
                    "skillsHotTier": {"frequent": 9}}}), encoding="utf-8")
    config = load_config(config_path)
    save_config(config, config_path)
    from durin.config.loader import read_persisted_config
    saved = read_persisted_config(config_path)
    assert "skillImport" not in saved.get("memory", {})
    assert "skillsHotTier" not in saved.get("memory", {})
```

- [ ] **Step 2: Run — expect FAIL** (`skillImport` survives under memory / new paths empty)

Run: `.venv/bin/python -m pytest tests/config/test_config_migration.py -q -k "skill"`

- [ ] **Step 3: Implement the migration** in `_migrate_config` (before `return data`)

```python
    # Move memory.skillImport → skills.security and memory.skillsHotTier →
    # agents.defaults.skillsHotTier (spec 2026-06-03 §9). Handle camel + snake.
    memory = data.get("memory", {})
    for legacy in ("skillImport", "skill_import"):
        if legacy in memory:
            security = data.setdefault("skills", {}).setdefault("security", {})
            for k, v in memory.pop(legacy).items():
                security.setdefault(k, v)
            break
    for legacy in ("skillsHotTier", "skills_hot_tier"):
        if legacy in memory:
            defaults = data.setdefault("agents", {}).setdefault("defaults", {})
            defaults.setdefault("skillsHotTier", memory.pop(legacy))
            break
```

- [ ] **Step 4: Run — expect PASS**

Run: `.venv/bin/python -m pytest tests/config/test_config_migration.py -q`

- [ ] **Step 5: Commit**

```bash
test "$(git -C /Users/marcelo/git_personal/durin branch --show-current)" = "skills-hot-tier" || { echo "BRANCH CHANGED"; exit 1; }
git -C /Users/marcelo/git_personal/durin add durin/config/loader.py tests/config/test_config_migration.py
git -C /Users/marcelo/git_personal/durin commit -m "feat(config): migrate legacy memory.skillImport/skillsHotTier to new paths"
```

---

### Task 3: Update Python consumers + existing tests to the new paths

**Files (verified consumers of `memory.skill_import` / `memory.skills_hot_tier`):**
- Modify: `durin/agent/skill_resolve.py:82`, `durin/agent/skills_store.py:450,458,467`, `durin/agent/tools/skill_import.py:97,101`, `durin/agent/tools/skill_audit.py:100` → `…skills.security…`
- Modify: `durin/agent/context.py:224` → `load_config().agents.defaults.skills_hot_tier`
- Modify existing tests: `tests/config/test_skill_import_config.py`, `tests/config/test_skills_hot_tier_config.py`, `tests/config/test_memory_config_skills.py` → new paths

- [ ] **Step 1: Find every reference** (catch anything the list missed)

Run: `grep -rn "memory\.skill_import\|\.skill_import\b\|memory\.skills_hot_tier\|\.skills_hot_tier\b" durin/ tests/ --include="*.py"`

- [ ] **Step 2: Update each consumer** — replace `<cfg>.memory.skill_import` with `<cfg>.skills.security` and `<cfg>.memory.skills_hot_tier` with `<cfg>.agents.defaults.skills_hot_tier`. In `tools/skill_import.py` `create()`, `ctx.app_config.memory.skill_import` → `ctx.app_config.skills.security`.

- [ ] **Step 3: Update the existing config tests** to assert/build the new paths (and where they round-trip through `load_config`, the migration keeps legacy-key tests valid — adjust assertions to the new location).

- [ ] **Step 4: Run the affected suites — expect PASS**

Run: `.venv/bin/python -m pytest tests/config/ tests/agent/ -q`

- [ ] **Step 5: Commit**

```bash
test "$(git -C /Users/marcelo/git_personal/durin branch --show-current)" = "skills-hot-tier" || { echo "BRANCH CHANGED"; exit 1; }
git -C /Users/marcelo/git_personal/durin add -A durin/ tests/
git -C /Users/marcelo/git_personal/durin commit -m "refactor(config): point skill consumers at skills.security / agents.defaults.skills_hot_tier"
```

---

### Task 4: Update webui config keys

**Files:**
- Modify: `webui/src/components/settings/SkillsSecuritySettings.tsx` (11 `"memory.skill_import.*"` onSave paths → `"skills.security.*"`)
- Modify: `webui/src/lib/api.ts:499` (`"memory.skill_import.allowlist"` → `"skills.security.allowlist"`)

- [ ] **Step 1: Replace the path strings** — every `memory.skill_import.` → `skills.security.` in both files (allowlist, llm_judge.trigger/max_severity/model, github_token_secret, max_files, max_total_bytes, max_file_bytes).

- [ ] **Step 2: Verify the webui builds** (tsc-in-build is the gate)

Run: `cd webui && bun run build`
Expected: build succeeds → `durin/web/dist`.

- [ ] **Step 3: Run webui tests**

Run: `cd webui && bun run test`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
test "$(git -C /Users/marcelo/git_personal/durin branch --show-current)" = "skills-hot-tier" || { echo "BRANCH CHANGED"; exit 1; }
git -C /Users/marcelo/git_personal/durin add webui/src durin/web/dist
git -C /Users/marcelo/git_personal/durin commit -m "refactor(webui): point skills security settings at skills.security config keys"
```

---

### Task 5: Full-suite green + live config round-trip

- [ ] **Step 1: Backend suite** — `.venv/bin/python -m pytest -q` (expect the prior baseline green; no new failures).
- [ ] **Step 2: Live round-trip** — write a `config.json` with legacy `memory.skillImport.allowlist`, `load_config`, assert it surfaces at `skills.security.allowlist`; `save_config`, confirm the file now has `skills.security` and no `memory.skillImport`.
- [ ] **Step 3: Grep clean** — `grep -rn "memory\.skill_import\|memory\.skills_hot_tier" durin/ webui/src/` returns only the migration shim in `loader.py` (legacy-key handling), nothing else.
