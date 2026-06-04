# Skill Interop Standard (§8.B) Implementation Plan

> ✅ **EXECUTED — historical execution record.** The feature this plan built is shipped and verified. Current as-built state: [`docs/architecture/skills/00_overview.md`](../../architecture/skills/00_overview.md). The unchecked `- [ ]` boxes below are the original TDD task list, **not pending work**.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make durin a *faithful* [agentskills.io](https://agentskills.io/specification) citizen so importing any standard `SKILL.md` is a near-no-op: preserve foreign frontmatter across edits (round-trip fidelity), honor the standard `platforms` field, accept standard spellings, and document the contract.

**Architecture:** durin is already ~90% compliant — `name`/`description` at root, durin behavior under `metadata.durin.*`, and `_update_md` (`split_frontmatter` → mutate → `join_frontmatter(sort_keys=False)`) is round-trip-safe. This plan locks that with tests and closes the one functional gap (`platforms`) plus small spelling/surfacing items. No format migration of existing skills; no import command (that's the next plan).

**Spec (source of truth):** [`docs/superpowers/specs/2026-06-03-skill-interop-standard-design.md`](../specs/2026-06-03-skill-interop-standard-design.md).

**Branch:** `skills-hot-tier` (current). **Checkout:** `/Users/marcelo/git_personal/durin` (shared — verify `git branch --show-current` == `skills-hot-tier` before each commit).

**Test command:** `cd /Users/marcelo/git_personal/durin && /Users/marcelo/git_personal/durin/.venv/bin/python -m pytest <paths> -v`

---

## File Structure

| File | Change | Task |
|---|---|---|
| `tests/agent/test_skill_interop_roundtrip.py` | NEW — lock foreign-key preservation across every mutation | 1 |
| `durin/agent/skills.py` | `platforms` OS gating (`_platform_ok` + filter in `list_skills`); accept kebab `disable-model-invocation` | 2, 3 |
| `durin/agent/skills_store.py` | surface `version`/`license` in `list_skills_info` | 3 |
| `tests/agent/test_skills_loader.py` | platforms + kebab-disable tests | 2, 3 |
| `docs/architecture/skills/01_format_and_interop.md` | NEW — canonical durin `SKILL.md` contract | 4 |

---

## Task 1: Round-trip fidelity — foreign frontmatter survives every mutation

**Why:** the keystone of importability. If durin drops `metadata.hermes.*` / `license` / unknown keys on the first edit, imported skills silently degrade. `_update_md` is structurally safe; this task *proves* it across all real mutation entry points and fixes any that rewrite from scratch.

**Files:** Create `tests/agent/test_skill_interop_roundtrip.py`. (Source change ONLY if a mutation is found to drop keys.)

- [ ] **Step 1: Read** `durin/agent/skills_store.py` — the mutation functions: `dream_create_skill`, `apply_skill_edit`, `save_skill_content`, `set_mode`, `mark_curated`, `dream_fuse_skills`, and the helpers `_update_md`, `_stamp`/`ensure_durin`, `fork_on_write`. Confirm each routes writes through `_update_md` (split→mutate→join) or writes user-supplied full content verbatim. Note the exact call signatures + how a skill is made `manual` (so `apply_skill_edit` applies directly without the proposed-diff gate).

- [ ] **Step 2: Write the test.** A skill authored with the full agentskills.io surface + a foreign vendor block + an unknown root key; assert every foreign key survives each mutation byte-equivalently.

```python
# tests/agent/test_skill_interop_roundtrip.py
from durin.agent.skills_frontmatter import split_frontmatter
from durin.agent import skills_store as ss

FOREIGN_SKILL = """---
name: imported-thing
description: An imported standard skill.
version: 2.1.0
license: MIT
platforms: [linux, macos]
allowed-tools: Bash Read
compatibility: needs python>=3.10
metadata:
  hermes:
    tags: [qa, browser]
    requires_toolsets: [web]
  somevendor:
    custom: keep-me
x-unknown-root: preserve-this
---

# Imported Thing

Step 1. do the thing.
"""

_FOREIGN_KEYS = ("version", "license", "platforms", "allowed-tools",
                 "compatibility", "x-unknown-root")


def _assert_foreign_intact(text: str):
    data, _ = split_frontmatter(text)
    for k in _FOREIGN_KEYS:
        assert k in data, f"lost root key {k!r}"
    assert data["version"] == "2.1.0"
    assert data["metadata"]["hermes"]["tags"] == ["qa", "browser"]
    assert data["metadata"]["hermes"]["requires_toolsets"] == ["web"]
    assert data["metadata"]["somevendor"]["custom"] == "keep-me"
    assert data["x-unknown-root"] == "preserve-this"


def _seed(workspace, name="imported-thing", content=FOREIGN_SKILL):
    d = workspace / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(content, encoding="utf-8")


def _read(workspace, name="imported-thing"):
    return (workspace / "skills" / name / "SKILL.md").read_text(encoding="utf-8")


def test_set_mode_preserves_foreign(tmp_path):
    _seed(tmp_path)
    ss.set_mode(tmp_path, "imported-thing", "manual")
    _assert_foreign_intact(_read(tmp_path))


def test_apply_edit_preserves_foreign(tmp_path):
    _seed(tmp_path)
    ss.set_mode(tmp_path, "imported-thing", "manual")
    # manual edit applies directly (ok=True path); adapt kwargs to the real signature
    ss.apply_skill_edit(tmp_path, name="imported-thing",
                        old="do the thing", new="do the improved thing",
                        rationale="tune", ok=True)
    text = _read(tmp_path)
    _assert_foreign_intact(text)
    assert "improved thing" in text


def test_mark_curated_preserves_foreign(tmp_path):
    _seed(tmp_path)
    ss.mark_curated(tmp_path, "imported-thing")
    _assert_foreign_intact(_read(tmp_path))


def test_save_content_preserves_what_user_writes(tmp_path):
    _seed(tmp_path)
    ss.set_mode(tmp_path, "imported-thing", "manual")
    ss.save_skill_content(tmp_path, "imported-thing", FOREIGN_SKILL, rationale="web edit")
    _assert_foreign_intact(_read(tmp_path))


def test_durin_namespace_added_without_clobbering(tmp_path):
    _seed(tmp_path)
    ss.set_mode(tmp_path, "imported-thing", "auto")
    data, _ = split_frontmatter(_read(tmp_path))
    assert data["metadata"]["durin"]["mode"] == "auto"   # durin namespace added
    assert data["metadata"]["hermes"]["tags"] == ["qa", "browser"]  # sibling untouched
```
Adapt every `ss.*` call to the REAL signature read in Step 1 (especially `apply_skill_edit`'s param names and the manual-mode `ok`/approval flag — match reality; if a fresh-authored skill defaults to `manual` or `auto`, adjust `set_mode` calls).

- [ ] **Step 3: Run.** `cd /Users/marcelo/git_personal/durin && /Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_skill_interop_roundtrip.py -v`. Expected: PASS (the design preserves keys). **If any test FAILS**, the named mutation drops keys — fix that function to route through `_update_md` / preserve the parsed dict, then re-run. Report which (if any) needed a fix.

- [ ] **Step 4: Commit** (verify branch first):
```bash
cd /Users/marcelo/git_personal/durin
git branch --show-current   # must be skills-hot-tier
git add tests/agent/test_skill_interop_roundtrip.py
# add durin/agent/skills_store.py too IF a fix was needed
git commit -m "test(skills): lock round-trip fidelity — foreign frontmatter survives mutations (agentskills.io interop)"
```

---

## Task 2: Honor the standard `platforms` field (OS gating)

**Why:** `platforms: [macos|linux|windows]` is the one *standard root* field with runtime semantics durin doesn't honor — an imported `platforms: [linux]` skill would wrongly appear on macOS. Per the standard (and Hermes/OpenClaw), an off-platform skill is **hidden entirely** (not shown as "unavailable").

**Files:** Modify `durin/agent/skills.py`; Test `tests/agent/test_skills_loader.py`.

- [ ] **Step 1: Read** `durin/agent/skills.py`: `list_skills` (~line 51), `_check_requirements` (~231), `get_skill_metadata` (~257), and the imports (`import os, shutil`). Confirm `list_skills` builds `skills` (workspace + builtin) then applies the `disabled_skills` + `filter_unavailable` filters.

- [ ] **Step 2: Write failing tests** in `tests/agent/test_skills_loader.py` (reuse the file's skill-writing helper; `platforms` is a ROOT frontmatter field):
```python
def test_platforms_hides_skill_off_platform(tmp_path, monkeypatch):
    import durin.agent.skills as skmod
    # skill restricted to linux
    _write_skill(tmp_path / "skills", "linux-only",
                 body="x", extra_frontmatter="platforms: [linux]")  # adapt helper
    loader = skmod.SkillsLoader(tmp_path)
    monkeypatch.setattr(skmod, "_current_platform", lambda: "macos")
    names = [s["name"] for s in loader.list_skills(filter_unavailable=False)]
    assert "linux-only" not in names
    monkeypatch.setattr(skmod, "_current_platform", lambda: "linux")
    names = [s["name"] for s in loader.list_skills(filter_unavailable=False)]
    assert "linux-only" in names


def test_platforms_accepts_openclaw_aliases(tmp_path, monkeypatch):
    import durin.agent.skills as skmod
    _write_skill(tmp_path / "skills", "mac-skill",
                 body="x", extra_frontmatter="platforms: [darwin]")  # openclaw alias
    loader = skmod.SkillsLoader(tmp_path)
    monkeypatch.setattr(skmod, "_current_platform", lambda: "macos")
    names = [s["name"] for s in loader.list_skills(filter_unavailable=False)]
    assert "mac-skill" in names   # darwin → macos


def test_no_platforms_means_all(tmp_path, monkeypatch):
    import durin.agent.skills as skmod
    _write_skill(tmp_path / "skills", "anywhere", body="x")
    loader = skmod.SkillsLoader(tmp_path)
    monkeypatch.setattr(skmod, "_current_platform", lambda: "windows")
    names = [s["name"] for s in loader.list_skills(filter_unavailable=False)]
    assert "anywhere" in names
```
Adapt `_write_skill(...)` to the real helper in the file (it may take a metadata/frontmatter arg differently — if it only writes `name`/`description`, extend it or write the SKILL.md directly with a `platforms:` line). Run → FAIL.

- [ ] **Step 3: Implement** in `durin/agent/skills.py`. Add module-level helpers near the top (after imports — `import sys` if absent):
```python
_PLATFORM_ALIASES = {
    "darwin": "macos", "macos": "macos", "osx": "macos", "mac": "macos",
    "linux": "linux",
    "win32": "windows", "windows": "windows", "win": "windows",
}


def _current_platform() -> str:
    p = sys.platform
    if p.startswith("linux"):
        return "linux"
    if p == "darwin":
        return "macos"
    if p.startswith("win"):
        return "windows"
    return p
```
Add a method on `SkillsLoader`:
```python
    def _platform_ok(self, name: str) -> bool:
        """Honor the agentskills.io root ``platforms`` field. No field = all
        platforms. Accepts standard (macos/linux/windows) + OpenClaw aliases
        (darwin/win32)."""
        meta = self.get_skill_metadata(name) or {}
        plats = meta.get("platforms")
        if not plats:
            return True
        if isinstance(plats, str):
            plats = [plats]
        normalized = {
            _PLATFORM_ALIASES.get(str(p).lower().strip(), str(p).lower().strip())
            for p in plats
        }
        return _current_platform() in normalized
```
In `list_skills`, after the workspace+builtin list is assembled and the `disabled_skills` filter, BEFORE the `filter_unavailable` return, add a hard platform filter (applies regardless of `filter_unavailable`):
```python
        skills = [s for s in skills if self._platform_ok(s["name"])]
```
(Place it right after the `if self.disabled_skills:` block.)

- [ ] **Step 4: Run** the new tests → PASS. Regression: `... -m pytest tests/agent/test_skills_loader.py -q` → no regression.

- [ ] **Step 5: Commit** (verify branch):
```bash
cd /Users/marcelo/git_personal/durin
git branch --show-current   # skills-hot-tier
git add durin/agent/skills.py tests/agent/test_skills_loader.py
git commit -m "feat(skills): honor agentskills.io platforms field (OS gating + openclaw aliases)"
```

---

## Task 3: Accept standard spellings + surface `version`/`license`

**Why:** import-compat for OpenClaw's root kebab `disable-model-invocation`, and make `version`/`license` (preserved by round-trip) visible so imported skills' provenance/versioning surfaces.

**Files:** Modify `durin/agent/skills.py` (`_is_model_invocation_disabled`), `durin/agent/skills_store.py` (`list_skills_info`); Test `tests/agent/test_skills_loader.py` + `tests/agent/test_skills_store_read.py` (or wherever `list_skills_info` is tested — grep).

- [ ] **Step 1: Read** `_is_model_invocation_disabled` (`skills.py` ~160) and `list_skills_info` (`skills_store.py`). Confirm the current keys.

- [ ] **Step 2: Write failing tests.**
```python
# in test_skills_loader.py
def test_kebab_disable_model_invocation_respected(tmp_path):
    from durin.agent.skills import SkillsLoader
    _write_skill(tmp_path / "skills", "hidden",
                 body="x", extra_frontmatter="disable-model-invocation: true")
    loader = SkillsLoader(tmp_path)
    summary = loader.build_skills_summary()
    assert "hidden" not in summary   # kebab form honored, like the snake/camel forms
```
```python
# in the test file covering list_skills_info (grep -rn "list_skills_info" tests/)
def test_list_skills_info_surfaces_version_and_license(tmp_path):
    from durin.agent.skills_store import list_skills_info
    d = tmp_path / "skills" / "v"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: v\ndescription: d\nversion: 3.4.5\nlicense: Apache-2.0\n---\nbody\n")
    info = {i["name"]: i for i in list_skills_info(tmp_path)}
    assert info["v"]["version"] == "3.4.5"
    assert info["v"]["license"] == "Apache-2.0"
```
Run → FAIL.

- [ ] **Step 3: Implement.**
In `skills.py` `_is_model_invocation_disabled`, add the kebab key to the OR:
```python
        return bool(
            skill_meta.get("disable_model_invocation")
            or skill_meta.get("disableModelInvocation")
            or skill_meta.get("disable-model-invocation")
        )
```
In `skills_store.py` `list_skills_info`, add to the appended dict (read from root `data`):
```python
            "version": data.get("version", ""),
            "license": data.get("license", ""),
```

- [ ] **Step 4: Run** both tests → PASS. Regression: `... -m pytest tests/agent/test_skills_loader.py tests/agent/test_skills_store_read.py -q` (adapt to the real list_skills_info test file) → no regression.

- [ ] **Step 5: Commit** (verify branch):
```bash
cd /Users/marcelo/git_personal/durin
git branch --show-current   # skills-hot-tier
git add durin/agent/skills.py durin/agent/skills_store.py tests/agent/test_skills_loader.py <list_skills_info test file>
git commit -m "feat(skills): accept kebab disable-model-invocation + surface version/license (interop)"
```

---

## Task 4: Document the canonical durin `SKILL.md` contract

**Why:** "fijar el estándar" = a written, canonical contract so every future surface (import, export, validation) references one source of truth.

**Files:** Create `docs/architecture/skills/01_format_and_interop.md`.

- [ ] **Step 1: Write** the doc. Content (fill from the spec — this is documentation, write it complete, no placeholders):
  - **Header:** durin's `SKILL.md` = the [agentskills.io](https://agentskills.io/specification) open standard + the `metadata.durin.*` vendor namespace.
  - **Root frontmatter (the standard):** `name` (required, 1-64 lowercase/digits/hyphens, matches dir), `description` (required, ≤1024), and optional `version`, `license`, `compatibility`, `allowed-tools`, `platforms: [macos|linux|windows]` (honored for OS gating; `darwin`/`win32` aliases accepted), root `disable-model-invocation`/`disable_model_invocation`/`disableModelInvocation` (all honored).
  - **`metadata.durin.*` (durin behavior):** `mode` (`manual`|`auto`), `provenance` (`source`, `created_at`), `requires` (`bins`, `env`), `always`, `curated`. Table each with meaning.
  - **Round-trip guarantee:** durin preserves every unknown root key and every foreign `metadata.<vendor>` block across edits (`split_frontmatter`/`join_frontmatter`/`_update_md`) — so an imported skill never loses data and can round-trip back to its origin. Cite `tests/agent/test_skill_interop_roundtrip.py` as the enforcement.
  - **Directory layout:** `workspace/skills/<name>/SKILL.md` + optional `references/`, `scripts/`, `assets/`, `templates/` (preserved/copied on import; reachable via `read_file`).
  - **Import posture (forward ref):** any agentskills.io skill drops in and works; import stamps `metadata.durin.provenance.source` + `mode` and may map foreign requirement fields → `metadata.durin.requires`. (Full import = the §6.B plan.)
  - **What durin does NOT honor (preserved-but-ignored):** other vendors' `metadata.<vendor>.*` behavior (e.g. Hermes toolset-conditional activation), `allowed-tools` enforcement (advisory).
- [ ] **Step 2: Commit** (verify branch):
```bash
cd /Users/marcelo/git_personal/durin
git branch --show-current   # skills-hot-tier
git add docs/architecture/skills/01_format_and_interop.md
git commit -m "docs(skills): canonical SKILL.md contract — agentskills.io + metadata.durin"
```

---

## Self-Review

**Spec coverage:** §5.1 round-trip fidelity → Task 1. §5.2 platforms → Task 2. §5.3 spellings + version/license → Task 3. §5.4 contract doc → Task 4. Deferred items (§6.B import, §6.C/D, §8.C/D/F) explicitly out of scope.

**No placeholders:** load-bearing code (`_platform_ok`, `_current_platform`, kebab key, version/license surfacing) shown in full; test bodies given; the only "adapt to real" notes are signatures the implementer must read first (`apply_skill_edit` params, the `_write_skill` test helper) — flagged explicitly.

**Type consistency:** `_current_platform()` and `_platform_ok` defined in Task 2, monkeypatched by name in tests. `_PLATFORM_ALIASES` keys lowercased; lookups lowercase+strip. `list_skills_info` dict gains `version`/`license` string fields (default `""`).

**Verify-live note:** after Task 2/3, drop a REAL Hermes skill (e.g. copy one from `/Users/marcelo/git_personal/hermes-agent/skills/*/SKILL.md`) into a temp workspace, load it, edit it, and confirm `metadata.hermes.*` + `version`/`license` survive and `platforms` gates correctly — the §7 success criteria.
