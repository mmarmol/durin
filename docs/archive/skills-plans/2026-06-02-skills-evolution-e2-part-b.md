# Skills Evolution E2 — Part B Implementation Plan

> ✅ **EXECUTED — historical execution record.** The feature this plan built is shipped and verified. Current as-built state: [`docs/architecture/skills/00_overview.md`](../../architecture/skills/00_overview.md). The unchecked `- [ ]` boxes below are the original TDD task list, **not pending work**.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The **daily dream** curates the **whole skill catalog by CONTENT** —
evolve a skill, or fuse overlapping ones — looking at all `auto` skills (the
day's usage is light context, NOT the driver). Plus a lead-in fix (Task 0) that
bounds the 2h dream's usage signal to recent sessions.

**Design principle (Hermes-grounded, 2026-06-02):** Hermes's periodic curator
explicitly forbids gating curation on usage counts (*"Judge overlap on CONTENT,
not use_count; use=0 is not evidence a skill is valuable"*). So Part B is
**content-driven**: it reads the skills themselves and decides by their content.
Usage (the day's `skill_calls`) is only context. The per-skill cursor
(`dream_processed_through`) is **only** anti-oscillation (skip skills curated
recently), never a value/usage gate.

**Architecture:** A new content-driven curation pass runs as a **separate step in
the daily `memory_dream` cron handler**, alongside (not inside) the per-entity
`DreamRunner`. It lists all `auto` skills, picks those whose per-skill cursor is
stale, hands their content to an LLM judge that proposes evolve/fuse actions, and
applies them via `skills_store` (commit + fork-on-write; `manual` untouched).

**Tech Stack:** Python, pytest (`tmp_path`), `skills_store` (frontmatter +
GitStore), the memory model/LLM-invoke used by `memory_dream`.

**Spec (source of truth):** [`docs/superpowers/specs/2026-06-02-skills-evolution-e2-design.md`](../specs/2026-06-02-skills-evolution-e2-design.md) §6, §7.

**Scope note:** Part B is **larger and riskier** than Part A — it runs an LLM over
the catalog and can *delete* skills (fusion removes the sources). Mitigation:
every write goes through `skills_store` (one git commit per action → `git revert`
recovers); `manual` skills are read-only; conservative fusion (only clear
content overlap).

---

## Task 0: Bound the 2h dream's usage signal to recent

**Why (Hermes):** the reactive path reacts to *recent* work, not all-time usage.
`collect_recent_skill_calls` currently reads every sidecar. Bound it to recent —
as a presence pointer ("which skills were active lately"), not a count ranking.

**Files:**
- Modify: `durin/agent/skill_usage.py` (`collect_recent_skill_calls`)
- Modify: `durin/agent/memory.py` (the 2h `Dream` call site)
- Test: `tests/agent/test_skill_usage.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_skill_usage.py  (add)
import os, time
from durin.agent.skill_usage import collect_recent_skill_calls
from durin.session.manager import SessionManager


def test_collect_skill_calls_within_hours_filters_old_sidecars(tmp_path):
    sm = SessionManager(tmp_path)
    for key in ("websocket:recent", "websocket:old"):
        s = sm.get_or_create(key)
        s.add_message("user", "x")
        s.metadata["skill_calls"] = [{"skill": "git-helper", "op": "read"}]
        sm.save(s)
    # Age the "old" sidecar to 100h ago.
    old_meta = next(p for p in (tmp_path / "sessions").glob("*old*.meta.json"))
    old = time.time() - 100 * 3600
    os.utime(old_meta, (old, old))

    # Unbounded: both counted (2). Bounded to 48h: only the recent one (1).
    assert collect_recent_skill_calls(tmp_path)["git-helper"]["read"] == 2
    assert collect_recent_skill_calls(tmp_path, within_hours=48)["git-helper"]["read"] == 1
```

- [ ] **Step 2: Run test → fail**

Run: `pytest tests/agent/test_skill_usage.py::test_collect_skill_calls_within_hours_filters_old_sidecars -v`
Expected: FAIL — `collect_recent_skill_calls() got an unexpected keyword argument 'within_hours'`

- [ ] **Step 3: Add the optional bound**

In `durin/agent/skill_usage.py`, change the signature and add an mtime filter
(default `None` = unbounded, preserves existing callers/tests):

```python
def collect_recent_skill_calls(workspace, within_hours: float | None = None) -> dict[str, dict[str, int]]:
    """Aggregate skill_calls across session sidecars: {skill: {op: count}}.

    ``within_hours`` (when set) only reads sidecars modified in that window —
    the 2h dream uses it to focus on *recent* activity (presence, not a
    count ranking). ``None`` reads all present sidecars.
    """
    from pathlib import Path
    import time as _time
    from durin.session.session_meta import read_derived

    workspace = Path(workspace)
    sessions_dir = workspace / "sessions"
    agg: dict[str, dict[str, int]] = {}
    if not sessions_dir.is_dir():
        return agg
    cutoff = (_time.time() - within_hours * 3600) if within_hours is not None else None
    for meta in sessions_dir.glob("*.meta.json"):
        try:
            if cutoff is not None and meta.stat().st_mtime < cutoff:
                continue
            derived = read_derived(meta)
        except Exception:
            continue
        for call in (derived.get("skill_calls") or []):
            skill = call.get("skill")
            op = call.get("op")
            if not skill or not op:
                continue
            agg.setdefault(skill, {}).setdefault(op, 0)
            agg[skill][op] += 1
    return agg
```

- [ ] **Step 4: Run test → pass**

Run: `pytest tests/agent/test_skill_usage.py -v`
Expected: PASS (all, incl. existing unbounded tests)

- [ ] **Step 5: 2h dream passes a recent window**

In `durin/agent/memory.py`, the 2h `Dream` call site (where it builds the
"Recently-Used Skills" block) — pass `within_hours`:

```python
        _used = collect_recent_skill_calls(self.store.workspace, within_hours=48)
```

(48h: generous enough to catch skills used across the last several 2h passes,
tight enough to stay "recent". Tune later via config if needed.)

- [ ] **Step 6: Commit**

```bash
git add durin/agent/skill_usage.py durin/agent/memory.py tests/agent/test_skill_usage.py
git commit -m "fix(skills): bound 2h dream usage signal to recent sidecars (48h)"
```

---

## Task 1: Per-skill curation cursor (`dream_processed_through` = content hash)

**Cut-off = CHANGE, not time.** The reason to review a skill is that it *changed*
(or is new) — a stable skill nobody touched has no reason to be re-reviewed, no
matter how many days passed. So the cursor stores the **content hash** with which
the skill was last reviewed. The daily pass reviews only the **delta**: skills
with no cursor (new) or whose current hash ≠ the stored hash (changed). Stable
skills (hash matches) are skipped with **no LLM call** → the pass scales to
thousands of skills (stable catalog → empty delta → no-op).

**Files:**
- Modify: `durin/agent/skills_store.py`
- Test: `tests/agent/test_skills_store_cursor.py`

**Hash the BODY, not the whole file.** Stamping the cursor lives in the
frontmatter — if we hashed the whole file, stamping would change the hash and the
skill would look "changed" forever. Hash only the body (post-frontmatter); that's
also the right signal (a curation-worthy change is a body change, not a mode flip).

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_skills_store_cursor.py
from durin.agent import skills_store as ss


def test_needs_curation_tracks_body_changes(tmp_path):
    ws = tmp_path / "ws"
    d = ws / "skills" / "git-helper"; d.mkdir(parents=True)
    md = d / "SKILL.md"
    md.write_text("---\nname: git-helper\n---\nv1 body\n", encoding="utf-8")

    assert ss.needs_curation(ws, "git-helper") is True          # never reviewed
    ss.mark_curated(ws, "git-helper")                            # stamp body hash
    assert ss.needs_curation(ws, "git-helper") is False         # unchanged → skip
    # stamping (frontmatter) must NOT re-trigger:
    assert ss.needs_curation(ws, "git-helper") is False

    md.write_text("---\nname: git-helper\n---\nv2 changed\n", encoding="utf-8")
    assert ss.needs_curation(ws, "git-helper") is True          # body changed
```

- [ ] **Step 2: Run → fail** (`AttributeError: needs_curation`)

Run: `pytest tests/agent/test_skills_store_cursor.py -v`

- [ ] **Step 3: Implement (read `_durin_blob`, `_update_md`, `ensure_durin`, `_store_init`, `split_frontmatter` first)**

```python
import hashlib


def _body_hash(text: str) -> str:
    _data, body = split_frontmatter(text)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def needs_curation(workspace: Path, name: str) -> bool:
    """True when the skill is new or its BODY changed since last curated."""
    text = read_skill_content(workspace, name)
    if text is None:
        return False
    prov = _durin_blob(text).get("provenance")
    stored = prov.get("dream_processed_through") if isinstance(prov, dict) else None
    return stored != _body_hash(text)


def mark_curated(workspace: Path, name: str) -> str | None:
    """Stamp provenance.dream_processed_through = current body hash + commit."""
    if not _safe_name(name):
        return None
    store = _store_init(workspace)
    dest = fork_on_write(workspace, name)
    h = _body_hash((dest / "SKILL.md").read_text(encoding="utf-8"))

    def _set(data: dict) -> None:
        durin = ensure_durin(data)
        prov = durin.get("provenance")
        if not isinstance(prov, dict):
            prov = {"source": "unknown", "created_at": _today()}
        prov["dream_processed_through"] = h
        durin["provenance"] = prov

    _update_md(dest / "SKILL.md", _set)
    return store.auto_commit(f"skill({name}): curated @ {h}")
```

- [ ] **Step 4: Run → pass**

Run: `pytest tests/agent/test_skills_store_cursor.py -v`

- [ ] **Step 5: Commit**

```bash
git add durin/agent/skills_store.py tests/agent/test_skills_store_cursor.py
git commit -m "feat(skills): per-skill curation cursor (dream_processed_through)"
```

---

## Task 2: Fusion helper (`dream_fuse_skills`)

Write the merged skill C, remove the sources A/B (workspace) or disable
(builtin), one atomic commit.

**Files:**
- Modify: `durin/agent/skills_store.py`
- Test: `tests/agent/test_skills_store_fuse.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_skills_store_fuse.py
from durin.agent import skills_store as ss


def _mk(ws, name):
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody {name}\n", encoding="utf-8")


def test_fuse_writes_c_removes_sources(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "git-a")
    _mk(ws, "git-b")
    res = ss.dream_fuse_skills(
        ws, target="git-flow", content="# Git flow\n\nmerged\n",
        sources=["git-a", "git-b"], rationale="overlap",
    )
    assert res.get("ok") is True
    assert (ws / "skills" / "git-flow" / "SKILL.md").exists()
    assert not (ws / "skills" / "git-a").exists()
    assert not (ws / "skills" / "git-b").exists()


def test_fuse_refuses_manual_source(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "git-a")
    d = ws / "skills" / "mine"; d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: mine\nmetadata:\n  durin:\n    mode: manual\n---\nx\n", encoding="utf-8"
    )
    res = ss.dream_fuse_skills(ws, target="c", content="x",
                               sources=["git-a", "mine"], rationale="r")
    assert "error" in res
    assert (ws / "skills" / "git-a").exists()  # nothing removed on refusal
```

- [ ] **Step 2: Run → fail** (`AttributeError: dream_fuse_skills`)

Run: `pytest tests/agent/test_skills_store_fuse.py -v`

- [ ] **Step 3: Implement**

```python
import shutil as _shutil  # (reuse the module's existing `shutil` import)


def dream_fuse_skills(workspace: Path, *, target: str, content: str,
                      sources: list[str], rationale: str) -> dict:
    """Fuse `sources` into a new `target` skill. Refuses if any source is
    `manual`. Writes target (source=dream, mode=auto), removes workspace
    sources / disables builtin sources, one commit."""
    if not _safe_name(target) or not all(_safe_name(s) for s in sources):
        return {"error": "invalid skill name"}
    if not rationale.strip():
        return {"error": "rationale is required"}
    for s in sources:
        if read_mode(workspace, s) == "manual":
            return {"error": f"source is manual, refusing: {s}"}
    if _skill_md(workspace, target).exists():
        return {"error": f"target already exists: {target}"}
    store = _store_init(workspace)
    # write target
    md = _skill_md(workspace, target)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(content, encoding="utf-8")

    def _stamp(data: dict) -> None:
        durin = ensure_durin(data)
        durin["mode"] = "auto"
        durin["provenance"] = {
            "source": "dream", "created_at": _today(),
            "fused_from": list(sources),
        }

    _update_md(md, _stamp)
    # remove/disable sources
    for s in sources:
        src_dir = _skills_dir(workspace) / s
        if src_dir.exists():
            _shutil.rmtree(src_dir)
        # builtin (no workspace dir): leave a tombstone disable in workspace
        else:
            tomb = _skills_dir(workspace) / s
            tomb.mkdir(parents=True, exist_ok=True)
            (tomb / "SKILL.md").write_text(
                f"---\nname: {s}\nmetadata:\n  durin:\n    mode: auto\n"
                f"    disable_model_invocation: true\n"
                f"    provenance:\n      source: dream\n      fused_into: {target}\n"
                f"---\nFused into `{target}`.\n", encoding="utf-8",
            )
    sha = store.auto_commit(f"skill: fuse {sources} → {target}: {rationale.strip()} [dream]")
    return {"ok": True, "target": target, "removed": list(sources), "commit": sha}
```

> Verify against the real `skills_store` helpers (`_skills_dir`, `_skill_md`,
> `read_mode`, `disable_model_invocation` frontmatter key from `skills.py`)
> before finalizing. Adjust the builtin-disable shape to what `SkillsLoader`
> actually honors.

- [ ] **Step 4: Run → pass**

Run: `pytest tests/agent/test_skills_store_fuse.py -v` then `pytest tests/agent/test_skills_store_security.py -v` (regression)

- [ ] **Step 5: Commit**

```bash
git add durin/agent/skills_store.py tests/agent/test_skills_store_fuse.py
git commit -m "feat(skills): dream_fuse_skills (merge sources → target, manual-safe)"
```

---

## Task 3: Curation core — content-driven, judge-injected

A function that selects stale `auto` skills, asks an injected LLM judge for
evolve/fuse actions over their **content**, applies via `skills_store`, stamps
cursors. Judge is injected so it's unit-testable with a fake.

**Files:**
- Create: `durin/agent/skill_curation.py`
- Test: `tests/agent/test_skill_curation.py`

- [ ] **Step 1: Write the failing test (fake judge)**

```python
# tests/agent/test_skill_curation.py
from durin.agent import skills_store as ss
from durin.agent.skill_curation import curate_catalog


def _mk(ws, name, body="body"):
    d = ws / "skills" / name; d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\nmetadata:\n  durin:\n    mode: auto\n"
        f"    provenance:\n      source: dream\n---\n{body}\n", encoding="utf-8")


def test_curate_reviews_only_the_changed_delta(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "changed", "new body")    # no cursor → in delta
    _mk(ws, "stable")
    ss.mark_curated(ws, "stable")      # body unchanged since → NOT in delta

    calls = []
    def fake_judge(prompt):
        calls.append(prompt); return '{"actions": []}'

    res = curate_catalog(ws, judge=fake_judge)
    assert res["reviewed"] == 1
    assert "changed" in calls[0] and "stable" not in calls[0]


def test_curate_fuses_when_judge_says_overlap(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "git-a", "git rebase steps")
    _mk(ws, "git-b", "git rebase steps too")

    def fake_judge(prompt: str) -> str:
        return '{"actions": [{"type": "fuse", "target": "git-flow", '\
               '"sources": ["git-a", "git-b"], "content": "# Git flow\\nmerged\\n", '\
               '"rationale": "same steps"}]}'

    res = curate_catalog(ws, judge=fake_judge)
    assert res["applied"] == 1
    assert (ws / "skills" / "git-flow" / "SKILL.md").exists()
    assert not (ws / "skills" / "git-a").exists()


def test_curate_budget_caps_delta_and_defers_rest(tmp_path):
    ws = tmp_path / "ws"
    for n in ("a", "b", "c"):
        _mk(ws, n)                     # 3 new → delta of 3
    res = curate_catalog(ws, judge=lambda p: '{"actions": []}', budget=2)
    assert res["reviewed"] == 2
    assert res["deferred"] == 1        # carried over (still no cursor → tomorrow)
```

- [ ] **Step 2: Run → fail** (module missing)

Run: `pytest tests/agent/test_skill_curation.py -v`

- [ ] **Step 3: Implement**

```python
# durin/agent/skill_curation.py
"""Daily content-driven skill curation (E2 Part B).

**Cut-off = CHANGE, not "review everything".** Reviews only the **delta** —
`auto` skills that are new or whose BODY changed since last curated (via
`skills_store.needs_curation`). Stable skills are skipped with no LLM call, so
the pass never scales with catalog size. A `budget` caps the per-day delta; the
rest carries over (they stay un-cursored → reviewed a later day, logged).

Asks an injected LLM judge to evolve/fuse the delta by CONTENT (Hermes rule: do
NOT gate on usage counts), applies via skills_store, stamps each reviewed skill's
cursor. The judge is a ``Callable[[str], str]`` returning JSON — injected so the
core is unit-testable without a provider. The day's usage is light context only.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from durin.agent import skills_store as ss

DEFAULT_BUDGET = 50
logger = logging.getLogger(__name__)


def curate_catalog(
    workspace: Path,
    *,
    judge: Callable[[str], str],
    usage: dict | None = None,
    budget: int = DEFAULT_BUDGET,
) -> dict:
    """One delta-curation pass. Returns {'reviewed', 'applied', 'deferred'}."""
    workspace = Path(workspace)
    auto = [s["name"] for s in ss.list_skills_info(workspace) if s["mode"] == "auto"]
    delta = [n for n in auto if ss.needs_curation(workspace, n)]   # ← the cut-off
    if not delta:
        return {"reviewed": 0, "applied": 0, "deferred": 0}

    selected = delta[:budget]
    deferred = len(delta) - len(selected)
    if deferred:
        logger.info("skill curation: delta=%d > budget=%d; deferring %d",
                    len(delta), budget, deferred)

    catalog = {n: ss.read_skill_content(workspace, n) or "" for n in selected}
    prompt = _build_prompt(catalog, usage or {})
    try:
        actions = (json.loads(judge(prompt)) or {}).get("actions", [])
    except (ValueError, TypeError):
        actions = []

    applied = 0
    for a in actions:
        t = a.get("type")
        if t == "fuse":
            r = ss.dream_fuse_skills(
                workspace, target=a["target"], content=a["content"],
                sources=a["sources"], rationale=a.get("rationale", "fuse"))
            applied += 1 if r.get("ok") else 0
        elif t == "evolve":
            r = ss.apply_skill_edit(
                workspace, a["name"], old=a["old"], new=a["new"],
                rationale=a.get("rationale", "evolve"))
            applied += 1 if r.get("ok") else 0

    # Mark reviewed skills (still present) at their current body hash, so an
    # unchanged body isn't re-reviewed; a fused-away source is skipped (None).
    for n in selected:
        if ss.read_skill_content(workspace, n) is not None:
            ss.mark_curated(workspace, n)
    return {"reviewed": len(selected), "applied": applied, "deferred": deferred}


def _build_prompt(catalog: dict, usage: dict) -> str:
    from durin.utils.prompt_templates import render_template
    return render_template(
        "agent/skill_curation.md", strip=True,
        catalog_json=json.dumps(catalog, ensure_ascii=False),
        usage_json=json.dumps(usage, ensure_ascii=False),
    )
```

> Verify `ss.apply_skill_edit`'s real kwargs (`old`/`new`/`rationale`) and
> `list_skills_info` shape against `skills_store.py`. If `render_template`
> isn't importable from that path, match the real helper used by the dream.

- [ ] **Step 4: Run → pass**

Run: `pytest tests/agent/test_skill_curation.py -v`

- [ ] **Step 5: Commit**

```bash
git add durin/agent/skill_curation.py tests/agent/test_skill_curation.py
git commit -m "feat(skills): content-driven daily catalog curation (judge-injected)"
```

---

## Task 4: Curation prompt template

**Files:**
- Create: `durin/templates/agent/skill_curation.md`
- Test: `tests/agent/test_skill_curation_prompt.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_skill_curation_prompt.py
from durin.utils.prompt_templates import render_template


def test_curation_prompt_renders_with_catalog_and_usage():
    out = render_template("agent/skill_curation.md", strip=True,
                          catalog_json='{"a": "body"}', usage_json='{}')
    assert "CONTENT" in out or "content" in out
    assert "fuse" in out and "evolve" in out
    assert '{"a": "body"}' in out
```

- [ ] **Step 2: Run → fail** (template missing)

Run: `pytest tests/agent/test_skill_curation_prompt.py -v`

- [ ] **Step 3: Write the template**

Create `durin/templates/agent/skill_curation.md`. It must: instruct judging by
**content** (explicitly: do NOT use usage counts to decide value — usage is only
context); define the JSON output `{"actions": [...]}` with `fuse`
(`target/sources/content/rationale`) and `evolve` (`name/old/new/rationale`);
be conservative (only fuse on clear overlap; never touch `manual`); and embed
`{{catalog_json}}` and `{{usage_json}}`. Match the template syntax used by the
other `durin/templates/agent/*.md` files (check `dream_phase2.md`).

- [ ] **Step 4: Run → pass**

Run: `pytest tests/agent/test_skill_curation_prompt.py -v`

- [ ] **Step 5: Commit**

```bash
git add durin/templates/agent/skill_curation.md tests/agent/test_skill_curation_prompt.py
git commit -m "feat(skills): catalog-curation prompt (content-driven, conservative)"
```

---

## Task 5: Wire into the daily `memory_dream` cron handler

**Files:**
- Modify: `durin/cli/commands.py` (the `memory_dream` branch of `on_cron_job`)
- Test: `tests/agent/test_skill_curation_wiring.py`

- [ ] **Step 1: Read the handler**

Read `durin/cli/commands.py` `on_cron_job` → `if job.name == "memory_dream":`.
Note how it resolves the model (`resolve_memory_model`) and builds the LLM
access (provider / `DreamRunner` llm_invoke). The curation step reuses that to
build a `judge` callable `(prompt) -> str` (a single LLM completion).

- [ ] **Step 2: Write the failing test (wiring is callable + safe on empty)**

```python
# tests/agent/test_skill_curation_wiring.py
from durin.agent.skill_curation import curate_catalog


def test_curate_noop_on_empty_catalog(tmp_path):
    calls = []
    res = curate_catalog(tmp_path / "ws",
                         judge=lambda p: calls.append(p) or '{"actions": []}')
    assert res == {"reviewed": 0, "applied": 0, "deferred": 0}
    assert calls == []  # judge never called when the delta is empty
```

Run: `pytest tests/agent/test_skill_curation_wiring.py -v` → PASS (pins the
no-op contract; the cron wiring itself is integration-verified by hand).

- [ ] **Step 3: Add the curation step to the `memory_dream` handler**

After the `DreamRunner` entity pass completes in the `memory_dream` branch, add
(guarded so failure never breaks the cron job):

```python
            try:
                from durin.agent.skill_curation import curate_catalog

                def _judge(prompt: str) -> str:
                    # one completion via the resolved memory model/provider
                    resp = provider.chat(model=mem_dream_cfg_model, messages=[
                        {"role": "user", "content": prompt}])
                    return resp.content or "{}"

                summary = curate_catalog(workspace, judge=_judge)
                logger.info("skill curation: reviewed=%s applied=%s",
                            summary["reviewed"], summary["applied"])
            except Exception:
                logger.exception("skill curation step failed (non-fatal)")
```

> Match the real provider/`chat` API + the real UTC-now helper in the repo (grep
> for how `memory.py`/`dream_runner.py` invoke the LLM and stamp timestamps).
> Reuse the already-resolved memory model from the handler, don't re-resolve.

- [ ] **Step 4: Run the test + a dream regression**

Run: `pytest tests/agent/test_skill_curation_wiring.py tests/agent/test_dream.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add durin/cli/commands.py tests/agent/test_skill_curation_wiring.py
git commit -m "feat(skills): run daily catalog curation in memory_dream cron"
```

---

## Self-review notes (for the implementer)

- **Content-driven, not count-driven (Hermes rule):** the judge prompt (Task 4)
  must judge by skill **content**; usage is context only. Do not add usage-count
  thresholds anywhere.
- **manual safety:** `dream_fuse_skills` refuses manual sources; `apply_skill_edit`
  already gates manual. The catalog list filters to `mode == auto`.
- **Reversibility:** every action is one `skills_store` commit → `git revert`
  recovers. Fusion removes sources only after the merged target is written, in
  the same commit.
- **Cut-off / scale (the daily never reviews "everything"):** the pass reviews
  only the **delta** — skills new or whose BODY hash ≠ their `dream_processed_through`.
  Stable skills cost nothing. `budget` caps the per-day delta; the rest carries
  over (logged). The whole catalog is fully traversed only **once** (first run,
  all un-cursored), then bounded to the delta forever. The cursor is a
  change-marker, **never** a value/usage gate.
- **Fusion at scale (Spec 2 dependency, noted):** comparing a delta skill against
  the whole catalog is cheap while the catalog is small; at thousands, the prompt
  should be fed only the delta's **similarity-neighbors** via the Spec 2 search
  index. Until Spec 2, the conservative bound is the `budget` + delta.
- **Adjust-to-real-API flags:** Tasks 2, 3, 5 carry explicit notes where the
  `skills_store` helper names, `apply_skill_edit` kwargs, builtin-disable
  frontmatter, `render_template` path, provider `chat` API, and UTC-now helper
  must be matched to the repo. Verify before finalizing — do not guess.
