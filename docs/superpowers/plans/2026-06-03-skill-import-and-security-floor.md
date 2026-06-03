# Skill Import (§6.B) + Security Floor (§8.C) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Checkbox (`- [ ]`) steps.

**Goal:** Import any agentskills.io skill (local path / URL / GitHub) into durin — source-agnostic — with an **invariant security floor**: a skill that carries code, or comes from outside the allowlist, **cannot be installed without explicit confirmation** (enforced in code, not by prompt).

**Architecture:** A deterministic core (fetch → quarantine → `validate_skill` lint + code-detection → `requires_confirmation` floor → `install_imported_skill` which refuses unconfirmed code/out-of-allowlist installs → commit + index) plus a builtin `import-skill` orchestrator skill that conducts it via the `skill_import` tool. Reuses the §8.B standard, E1 store, Spec-2 indexer.

**Spec (source of truth):** [`docs/superpowers/specs/2026-06-03-skill-import-and-security-floor-design.md`](../specs/2026-06-03-skill-import-and-security-floor-design.md).

**Branch:** `skills-hot-tier`. **Checkout:** `/Users/marcelo/git_personal/durin` (shared — verify `git branch --show-current` == `skills-hot-tier` before each commit).

**Test command:** `cd /Users/marcelo/git_personal/durin && /Users/marcelo/git_personal/durin/.venv/bin/python -m pytest <paths> -v`

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `durin/config/schema.py` | `MemoryConfig.skill_import: SkillImportConfig(allowlist: list[str] = [])` | 1 |
| `durin/agent/skills_import.py` (NEW) | `validate_skill`, `requires_confirmation`, `fetch_skill`, `install_imported_skill`, `SkillImportRefused` | 2,3,4,5 |
| `durin/agent/tools/skill_import.py` (NEW) | `skill_import` tool (report / gated install) | 6 |
| `durin/skills/import-skill/SKILL.md` (NEW) | builtin orchestrator skill | 7 |
| tests + verify | per task | all |

> **Config placement note (flag for review):** import config sits under `memory.skill_import` for consistency with `memory.skills_hot_tier`/`memory.index_skills` (skills are a memory class). Trivially relocatable to a top-level `skills` config later.

---

## Task 1: Config — `SkillImportConfig.allowlist`

**Files:** `durin/config/schema.py`; Test `tests/config/test_skill_import_config.py`.

- [ ] **Step 1: failing test**
```python
from durin.config.schema import Config

def test_default_allowlist_empty():
    assert Config().memory.skill_import.allowlist == []

def test_allowlist_camel_roundtrip():
    cfg = Config.model_validate({"memory": {"skillImport": {"allowlist": ["github:NousResearch/"]}}})
    assert cfg.memory.skill_import.allowlist == ["github:NousResearch/"]
```
- [ ] **Step 2: run → FAIL.**
- [ ] **Step 3: implement.** Add near the other `Memory*` configs (subclass the same `Base`):
```python
class SkillImportConfig(Base):
    """Security floor for skill import (§8.C). ``allowlist`` is a list of
    trusted source prefixes (e.g. ``github:anthropics/``). A source matching
    the allowlist skips the *source* confirmation; the *code-carrying*
    confirmation has no opt-out. Default empty = every source confirms once."""

    allowlist: list[str] = Field(default_factory=list)
```
Add to `MemoryConfig` (next to `skills_hot_tier`):
```python
    skill_import: SkillImportConfig = Field(default_factory=SkillImportConfig)
```
- [ ] **Step 4: run → PASS;** regression `tests/config/ -q`.
- [ ] **Step 5: commit** (verify branch): `git add durin/config/schema.py tests/config/test_skill_import_config.py && git commit -m "feat(config): memory.skill_import.allowlist (skill import security floor §8.C)"`

---

## Task 2: `validate_skill` — agentskills.io lint + code detection (deterministic)

**Files:** Create `durin/agent/skills_import.py`; Test `tests/agent/test_skill_validate.py`.

- [ ] **Step 1: failing test**
```python
from pathlib import Path
from durin.agent.skills_import import validate_skill

def _mk(tmp, name, fm="", body="x", scripts=None):
    d = tmp / name; d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n{fm}---\n{body}\n")
    if scripts:
        s = d / "scripts"; s.mkdir()
        for fn in scripts: (s / fn).write_text("#!/bin/sh\necho hi\n")
    return d

def test_valid_skill_no_code(tmp_path):
    r = validate_skill(_mk(tmp_path, "clean"))
    assert r.ok and not r.errors and r.carries_code is False

def test_missing_description_is_error(tmp_path):
    d = tmp_path / "bad"; d.mkdir()
    (d / "SKILL.md").write_text("---\nname: bad\n---\nx\n")
    r = validate_skill(d)
    assert not r.ok and any("description" in e for e in r.errors)

def test_scripts_dir_flags_code(tmp_path):
    r = validate_skill(_mk(tmp_path, "tool", scripts=["setup.sh"]))
    assert r.carries_code is True and "scripts/setup.sh" in r.code_artifacts

def test_install_spec_flags_code(tmp_path):
    fm = "metadata:\n  openclaw:\n    install:\n      - {kind: brew, formula: gh}\n"
    r = validate_skill(_mk(tmp_path, "ghskill", fm=fm))
    assert r.carries_code is True and any("install" in a for a in r.code_artifacts)

def test_nonconformant_name_is_warning_not_error(tmp_path):
    d = tmp_path / "Bad_Name"; d.mkdir()
    (d / "SKILL.md").write_text("---\nname: Bad_Name\ndescription: d\n---\nx\n")
    r = validate_skill(d)
    assert r.ok  # name issues are warnings (import-friendly), not blockers
    assert any("name" in w for w in r.warnings)
```
- [ ] **Step 2: run → FAIL.**
- [ ] **Step 3: implement** in `durin/agent/skills_import.py`:
```python
"""Skill import (§6.B) + security floor (§8.C). Deterministic core — the
LLM-facing orchestration lives in the builtin import-skill SKILL.md."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from durin.agent.skills_frontmatter import split_frontmatter

_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$")


@dataclass
class ValidationReport:
    name: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    carries_code: bool = False
    code_artifacts: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_skill(skill_dir: Path) -> ValidationReport:
    """Validate a skill dir against agentskills.io + detect code. Deterministic;
    name issues are WARNINGS (import-friendly), missing name/description are ERRORS."""
    skill_dir = Path(skill_dir)
    md = skill_dir / "SKILL.md"
    rep = ValidationReport(name=skill_dir.name)
    if not md.is_file():
        rep.errors.append("no SKILL.md")
        return rep
    data, _ = split_frontmatter(md.read_text(encoding="utf-8"))
    name = str(data.get("name") or "").strip()
    desc = str(data.get("description") or "").strip()
    if name:
        rep.name = name
        if not _NAME_RE.match(name):
            rep.warnings.append(f"name {name!r} not agentskills.io-conformant (1-64 lowercase/digits/hyphens)")
        if name != skill_dir.name:
            rep.warnings.append(f"name {name!r} != directory {skill_dir.name!r}")
    else:
        rep.errors.append("missing required 'name'")
    if not desc:
        rep.errors.append("missing required 'description'")
    elif len(desc) > 1024:
        rep.warnings.append("description exceeds 1024 chars")
    # --- code detection (deterministic) ---
    scripts = skill_dir / "scripts"
    if scripts.is_dir():
        for p in sorted(scripts.rglob("*")):
            if p.is_file():
                rep.code_artifacts.append(str(p.relative_to(skill_dir)))
    meta = data.get("metadata")
    if isinstance(meta, dict):
        for vendor, blob in meta.items():
            if isinstance(blob, dict) and blob.get("install"):
                rep.code_artifacts.append(f"metadata.{vendor}.install")
    rep.carries_code = bool(rep.code_artifacts)
    return rep
```
- [ ] **Step 4: run → PASS** (5 tests). **Step 5: commit** (verify branch): `git add durin/agent/skills_import.py tests/agent/test_skill_validate.py && git commit -m "feat(skills): validate_skill — agentskills.io lint + deterministic code detection"`

---

## Task 3: `requires_confirmation` — the §8.C floor decision (pure)

**Files:** `durin/agent/skills_import.py`; Test `tests/agent/test_skill_import_floor.py`.

- [ ] **Step 1: failing test**
```python
from durin.agent.skills_import import requires_confirmation, ValidationReport

def _rep(code=False, arts=None):
    return ValidationReport(name="s", carries_code=code, code_artifacts=arts or [])

def test_code_always_confirms_even_if_allowlisted():
    r = requires_confirmation("github:NousResearch/x", _rep(code=True, arts=["scripts/s.sh"]),
                              allowlist=["github:NousResearch/"])
    assert r and "code" in r          # code confirmation has NO opt-out

def test_out_of_allowlist_confirms_even_without_code():
    r = requires_confirmation("github:rando/x", _rep(code=False), allowlist=["github:NousResearch/"])
    assert r and "allowlist" in r

def test_allowlisted_no_code_no_confirmation():
    assert requires_confirmation("github:NousResearch/x", _rep(code=False),
                                 allowlist=["github:NousResearch/"]) is None
```
- [ ] **Step 2: run → FAIL. Step 3: implement** (append to `skills_import.py`):
```python
def _source_allowlisted(source: str, allowlist: list[str]) -> bool:
    return any(source.startswith(p) for p in allowlist if p)


def requires_confirmation(source: str, report: ValidationReport, *, allowlist: list[str]) -> str | None:
    """The §8.C invariant floor as a pure function. Returns a human reason when
    confirmation is required, else None. CODE confirmation has no opt-out;
    only the SOURCE check is loosened by the allowlist."""
    reasons: list[str] = []
    if report.carries_code:
        reasons.append("carries code (" + ", ".join(report.code_artifacts) + ")")
    if not _source_allowlisted(source, allowlist):
        reasons.append(f"source {source!r} not in allowlist")
    return "; ".join(reasons) if reasons else None
```
- [ ] **Step 4: run → PASS. Step 5: commit:** `git add durin/agent/skills_import.py tests/agent/test_skill_import_floor.py && git commit -m "feat(skills): requires_confirmation — §8.C invariant floor (code + out-of-allowlist)"`

---

## Task 4: `fetch_skill` — source adapters → quarantine

**Files:** `durin/agent/skills_import.py`; Test `tests/agent/test_skill_fetch.py`.

**Scope:** `path://<dir>` (local skill dir, first-class, fully tested). `https://…/SKILL.md` (single file → quarantine dir) and `github:owner/repo/path` (→ raw URL fetch) use a small HTTP fetch — STUB the network in tests; the local path is the only one exercised end-to-end here.

- [ ] **Step 1: failing test** (local path only — the deterministic one):
```python
from durin.agent.skills_import import fetch_skill

def test_fetch_local_dir_copies_to_quarantine(tmp_path):
    src = tmp_path / "src" / "mine"; src.mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: mine\ndescription: d\n---\nx\n")
    (src / "scripts").mkdir(); (src / "scripts" / "s.sh").write_text("echo\n")
    q = tmp_path / "q"
    out = fetch_skill(f"path://{src}", quarantine_root=q)
    assert (out / "SKILL.md").is_file() and (out / "scripts" / "s.sh").is_file()
    assert q in out.parents  # landed under quarantine, not active skills/
```
- [ ] **Step 2: run → FAIL. Step 3: implement.** Read how durin does HTTP elsewhere (`grep -rn "httpx\|requests\|urllib\|web_fetch" durin/ | head`) and reuse it for the URL/github branches; implement `path://` with `shutil.copytree`. Sketch:
```python
import shutil
from urllib.parse import urlparse

def fetch_skill(source: str, *, quarantine_root: Path) -> Path:
    """Fetch a skill into a quarantine dir. Returns the quarantined skill dir.
    Sources: path://<dir>, https://…/SKILL.md, github:owner/repo/path."""
    quarantine_root = Path(quarantine_root)
    quarantine_root.mkdir(parents=True, exist_ok=True)
    if source.startswith("path://"):
        src = Path(source[len("path://"):]).expanduser()
        if not (src / "SKILL.md").is_file():
            raise ValueError(f"no SKILL.md at {src}")
        dest = quarantine_root / src.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        return dest
    # https:// single SKILL.md  |  github:owner/repo/path → raw URL
    # ... fetch text via durin's HTTP helper; write quarantine_root/<name>/SKILL.md ...
    raise NotImplementedError("URL/github fetch — implement with durin's HTTP helper")
```
Implement the URL/github branches using the real HTTP helper you found; keep them minimal. (If durin has no HTTP helper, use `urllib.request` with a timeout; note that network branches are stubbed in tests.)
- [ ] **Step 4: run local test → PASS;** add a stubbed URL test if practical. **Step 5: commit:** `git add durin/agent/skills_import.py tests/agent/test_skill_fetch.py && git commit -m "feat(skills): fetch_skill — local/URL/github → quarantine"`

---

## Task 5: `install_imported_skill` — the gated install (THE INVARIANT)

**Files:** `durin/agent/skills_import.py`; Test `tests/agent/test_skill_install_gate.py`.

**This is the security keystone.** Install REFUSES when confirmation is required and not given.

- [ ] **Step 1: failing tests** (the invariant + the happy paths):
```python
import pytest
from durin.agent.skills_import import install_imported_skill, SkillImportRefused, fetch_skill
from durin.agent.skills_frontmatter import split_frontmatter

def _quarantine(tmp_path, name="mine", scripts=False):
    src = tmp_path / "src" / name; src.mkdir(parents=True)
    (src / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody\n")
    if scripts:
        (src / "scripts").mkdir(); (src / "scripts" / "s.sh").write_text("echo\n")
    return fetch_skill(f"path://{src}", quarantine_root=tmp_path / "q")

def test_code_skill_refused_without_confirmation(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    q = _quarantine(tmp_path, scripts=True)
    with pytest.raises(SkillImportRefused):
        install_imported_skill(ws, q, source="path://x", confirmed=False, allowlist=[])
    assert not (ws / "skills" / "mine").exists()   # NOT installed

def test_code_skill_installs_with_confirmation(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    q = _quarantine(tmp_path, scripts=True)
    res = install_imported_skill(ws, q, source="path://x", confirmed=True, allowlist=[])
    assert res.get("ok") and (ws / "skills" / "mine" / "scripts" / "s.sh").is_file()
    data, _ = split_frontmatter((ws / "skills" / "mine" / "SKILL.md").read_text())
    prov = data["metadata"]["durin"]["provenance"]
    assert prov["source"] == "path://x" and prov["carried_code"] is True
    assert data["metadata"]["durin"]["mode"] == "manual"

def test_clean_allowlisted_installs_without_confirmation(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    q = _quarantine(tmp_path, scripts=False)
    res = install_imported_skill(ws, q, source="github:NousResearch/x",
                                 confirmed=False, allowlist=["github:NousResearch/"])
    assert res.get("ok") and (ws / "skills" / "mine" / "SKILL.md").is_file()

def test_out_of_allowlist_no_code_refused_without_confirmation(tmp_path):
    ws = tmp_path / "ws"; ws.mkdir()
    q = _quarantine(tmp_path, scripts=False)
    with pytest.raises(SkillImportRefused):
        install_imported_skill(ws, q, source="github:rando/x", confirmed=False, allowlist=[])
```
- [ ] **Step 2: run → FAIL. Step 3: implement.** Read `durin/agent/skills_store.py` for `_safe_name`, `_store_init`, `_update_md`, `ensure_durin`, `_today`, `_sync_index` (import them or call via skills_store). Implement:
```python
class SkillImportRefused(Exception):
    """Raised when install is attempted without a required §8.C confirmation."""


def install_imported_skill(workspace, quarantine_dir, *, source: str, confirmed: bool,
                           allowlist: list[str], rationale: str = "import") -> dict:
    from durin.agent.skills_store import _safe_name, _store_init, _update_md, ensure_durin, _today
    from durin.memory.indexer import reindex_one_skill  # or skills_store._sync_index
    import shutil
    workspace = Path(workspace)
    quarantine_dir = Path(quarantine_dir)
    report = validate_skill(quarantine_dir)
    if report.errors:
        return {"error": f"invalid skill: {report.errors}"}
    name = report.name
    if not _safe_name(name):
        return {"error": f"unsafe skill name: {name!r}"}
    reason = requires_confirmation(source, report, allowlist=allowlist)
    if reason and not confirmed:
        raise SkillImportRefused(reason)            # <<< THE FLOOR
    dest = workspace / "skills" / name
    if dest.exists():
        return {"error": f"skill already exists: {name}"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(quarantine_dir, dest)
    def _stamp(data: dict) -> None:
        durin = ensure_durin(data)
        durin["mode"] = "manual"
        durin["provenance"] = {
            "source": source, "imported_at": _today(),
            "confirmed": bool(confirmed and reason), "carried_code": report.carries_code,
        }
    _update_md(dest / "SKILL.md", _stamp)
    store = _store_init(workspace)
    sha = store.auto_commit(f"skill({name}): {rationale} from {source}")
    # index sync (reuse the sanctioned path)
    from durin.agent import skills_store as _ss
    _ss._sync_index(workspace, name)
    return {"ok": True, "name": name, "commit": sha,
            "carried_code": report.carries_code, "warnings": report.warnings}
```
(Adapt the exact `_sync_index` / index call to the real skills_store symbol — read it. The point: reuse the sanctioned commit + index path, never raw writes.)
- [ ] **Step 4: run → PASS** (4 tests incl. both refusal invariants). Regression: `tests/agent/test_skills_store_*.py tests/agent/test_memory_search_skill.py -q`. **Step 5: commit:** `git add durin/agent/skills_import.py tests/agent/test_skill_install_gate.py && git commit -m "feat(skills): install_imported_skill — §8.C floor enforced in code (refuses unconfirmed code/out-of-allowlist)"`

---

## Task 6: `skill_import` tool

**Files:** Create `durin/agent/tools/skill_import.py`; Test `tests/agent/test_skill_import_tool.py`.

Mirror the `apply_skill_edit` tool + confirm pattern. `skill_import(source, confirm=false)`:
- fetch → quarantine, validate; if `requires_confirmation` and not confirm → return `{"needs_confirmation": <reason>, "code_artifacts": [...], "warnings": [...], "name": ...}` (NO install).
- else (or confirm=true) → `install_imported_skill(..., confirmed=confirm)`; on `SkillImportRefused` return `{"needs_confirmation": ...}`.

- [ ] **Step 1: Read** an existing tool (`durin/agent/tools/skill_edit.py` / `skill_write.py`) for the tool class shape (`_PARAMETERS`, `create(ctx)`, `execute`, how it reads `ctx.app_config`/workspace + the allowlist from `load_config().memory.skill_import.allowlist`).
- [ ] **Step 2: failing test** — local path code-skill: `confirm=False` → `needs_confirmation` + not installed; `confirm=True` → installed. Drive the tool's `execute` like other tool tests.
- [ ] **Step 3: implement** the tool (quarantine under `workspace/.durin/import-quarantine/`; read allowlist from config best-effort). - [ ] **Step 4: run + regression.** - [ ] **Step 5: commit:** `feat(skills): skill_import tool (fetch+lint+gated install)`

---

## Task 7: Builtin `import-skill` orchestrator skill

**Files:** Create `durin/skills/import-skill/SKILL.md`.

- [ ] **Step 1: Read** an existing builtin skill (`durin/skills/skill-creator/SKILL.md`) for the format/frontmatter durin builtins use.
- [ ] **Step 2: Write** `durin/skills/import-skill/SKILL.md` — frontmatter (`name: import-skill`, `description`, `metadata.durin.mode: auto`, provenance builtin), body procedure:
  1. Call `skill_import(source)`; read the lint report.
  2. If `needs_confirmation`: use `AskUserQuestion` to show the source + the exact `code_artifacts` + warnings, recommend the safe default, and get an explicit decision. Never auto-approve.
  3. Dedup: `memory_search(kind="skill")` for overlap; surface merge/keep/replace if a near-dup exists.
  4. *(Optional Etapa-2)* adapt foreign tool references to durin-native; opt-out = import as-is.
  5. `skill_import(source, confirm=true)` to install. Note explicitly: the code refuses unconfirmed code/out-of-allowlist installs, so the confirmation in step 2 is mandatory for those.
- [ ] **Step 3:** confirm it loads (a test that `SkillsLoader` lists `import-skill`, or it appears in the builtin set). **Step 4: commit:** `feat(skills): builtin import-skill orchestrator (§5.6 meta-skill seed)`

---

## Task 8: VERIFY LIVE (gate, no commit)

- [ ] Drive the real flow against a **real Hermes code-bearing skill** (e.g. one with `scripts/` under `/Users/marcelo/git_personal/hermes-agent/skills/`). Script: copy it to a temp source dir, `skill_import("path://…", confirm=False)` → assert `needs_confirmation` lists the scripts and **nothing installed**; `skill_import("path://…", confirm=True)` → installed, `scripts/` copied, provenance stamped `carried_code: true, mode: manual`, indexed (searchable via `memory_search(kind="skill")`). Then a **clean allowlisted** skill installs with no confirmation. Print `IMPORT LIVE: ALL PASS`.

---

## Self-Review

**Spec coverage:** §2 core → Tasks 2-5; §3 orchestrator skill → Task 7; §4 floor (invariant in code) → Tasks 3+5 (the refusal tests ARE the invariant). §5 scope honored (local/URL/github; no marketplace/installer-run/per-exec gating). Success criteria §7 → Task 8 + the gate tests.

**Security invariant is testable, not theatrical:** `install_imported_skill` raises `SkillImportRefused` when `requires_confirmation` is set and `confirmed` is False — proven by `test_code_skill_refused_without_confirmation` + `test_out_of_allowlist_no_code_refused_without_confirmation`. The agent cannot bypass it by ignoring the orchestrator skill.

**No placeholders:** full code for `validate_skill`, `requires_confirmation`, `install_imported_skill` (the security-load-bearing pieces); fetch URL/github + the tool reference the real HTTP/tool patterns the implementer must read first (flagged).

**Reuse, not reinvent:** install goes through `_store_init`/`auto_commit` + `_sync_index` (the sanctioned commit+index path), and `_safe_name` (traversal guard) — never raw file writes.
