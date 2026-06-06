# §6.C Acquire-on-gap Implementation Plan

> ✅ **EXECUTED — historical execution record.** The feature this plan built is shipped and verified. Current as-built state: [`docs/architecture/skills/00_overview.md`](../../architecture/skills/00_overview.md). The unchecked `- [ ]` boxes below are the original TDD task list, **not pending work**.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect durin's existing federated registry search to its existing skill-authoring path so durin acquires a skill on its own initiative — searching registries and using a safe hit as a seed — in-session (interactive) and in the 2h dream (autonomous, safe-only).

**Architecture:** Two paths over the same substrate (search → §8.C gate → author). **Path A (in-session)** is a prompt-only change: the in-loop agent already has `skill_search`, `skill_import`, `skill_write`, `ask_user_question` in core. **Path B (dream phase-2)** gets the raw `skill_search` tool (so the dream sees the full hit list — transparent discovery) plus a new per-ref `skill_acquire_seed(source)` tool: given ONE chosen ref it runs the §8.C gate and returns the SKILL.md body **only if `decide_action == "allow"`**, else refuses ("pick another"). The risk rule is thus enforced in code — the autonomous dream can never receive risky content. **Cost:** the gate's scan is a static regex pass; the LLM **judge is never used** here (`judge_trigger="off"`). A non-allowlisted ref can never be `allow`, so `skill_acquire_seed` **rejects it instantly without any download** — only allowlisted (user-trusted) refs are fetched. Risky hits in-session route to the user via `ask_user_question`.

**Tech Stack:** Python 3, pytest, durin's `Tool` base + `tool_parameters_schema`, existing `skill_registry` / `skills_import` / `skill_resolve` / `skill_scan` modules.

**Spec:** `docs/superpowers/specs/2026-06-03-skill-acquire-on-gap-design.md`

**Refines spec §5.1:** Path B uses a purpose-built gated `skill_acquire_seed` tool (code-enforced safe-only) instead of raw `skill_search` in the dream toolset. Everything else matches the spec.

**Conservative-by-default note (read before testing live):** `decide_action` returns
`allow` only when the source is **allowlisted** AND verdict is clean AND no code.
With the default empty `skills.security.allowlist`, every hit → `confirm` → no
autonomous seed. So Path B yields a seed live ONLY when the user has allowlisted a
source (e.g. a trusted github owner). This mirrors the drift design and is intended.

---

## File Structure

- **Create** `durin/agent/skill_acquire.py` — `acquire_safe_seed()`: the gated
  search→fetch→gate→safe-seed function (the only new logic). One responsibility.
- **Create** `durin/agent/tools/skill_acquire_seed.py` — `SkillAcquireSeedTool`:
  thin tool wrapper that pulls registries/allowlist/limit from config (same pattern
  as `SkillSearchTool`) and calls `acquire_safe_seed`.
- **Modify** `durin/agent/memory.py` (`Dream._build_tools`, ~line 1157) — register
  `SkillAcquireSeedTool` in the dream phase-2 toolset.
- **Modify** `durin/templates/agent/dream_phase2.md` — instruct phase-2 to try
  `skill_acquire_seed` before authoring a `[SKILL]` from scratch.
- **Modify** `durin/templates/agent/skills_section.md` — Path A: extend the
  always-rendered skills block with the acquire-on-gap workflow + risk rule.
- **Create** `tests/agent/test_skill_acquire.py` — unit tests for `acquire_safe_seed`.
- **Create** `tests/agent/test_skill_acquire_seed_tool.py` — tool wrapper + dream
  registration tests.

---

## Task 1: `acquire_safe_seed` — the gated seed function

**Files:**
- Create: `durin/agent/skill_acquire.py`
- Test: `tests/agent/test_skill_acquire.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/agent/test_skill_acquire.py
"""§6.C — acquire_safe_seed gates ONE ref; only a risk-free allowlisted ref seeds."""
import asyncio
from pathlib import Path

from durin.agent import skill_acquire


class _Cand:
    def __init__(self, name, ref):
        self.name, self.ref, self.kind = name, ref, "github"


class _Resolve:
    def __init__(self, cands):
        self.candidates = cands


class _Scan:
    def __init__(self, verdict):
        self._v = verdict

    @property
    def verdict(self):
        return self._v


class _Valid:
    def __init__(self, carries_code):
        self.carries_code = carries_code


def _wire(monkeypatch, *, verdict, carries_code, fetch_spy=None):
    """Patch resolve/fetch/scan so the test is offline + deterministic.
    decide_action is REAL (pure) — the allowlist gating is exercised for real."""
    def _resolve(ref):
        return _Resolve([_Cand(ref.split("/")[-1], ref)])

    def _fetch(cand, *, quarantine_root, allowlist=None, judge_trigger="off"):
        if fetch_spy is not None:
            fetch_spy.append(cand.ref)
        d = Path(quarantine_root) / cand.name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text("---\nname: x\n---\nbody", encoding="utf-8")
        return d

    monkeypatch.setattr("durin.agent.skill_resolve.resolve_candidates", _resolve)
    monkeypatch.setattr("durin.agent.skills_import.fetch_candidate", _fetch)
    monkeypatch.setattr("durin.agent.skills_import.validate_skill",
                        lambda d: _Valid(carries_code))
    monkeypatch.setattr("durin.security.skill_scan.scan_skill",
                        lambda d: _Scan(verdict))


def test_allowlisted_clean_ref_returns_seed(monkeypatch, tmp_path):
    _wire(monkeypatch, verdict="safe", carries_code=False)
    out = asyncio.run(skill_acquire.acquire_safe_seed(
        tmp_path, "github:acme/pdf", allowlist=["github:acme"]))
    assert out is not None
    assert out["source"] == "github:acme/pdf"
    assert "body" in out["content"]


def test_not_allowlisted_rejected_without_download(monkeypatch, tmp_path):
    spy: list[str] = []
    _wire(monkeypatch, verdict="safe", carries_code=False, fetch_spy=spy)
    out = asyncio.run(skill_acquire.acquire_safe_seed(
        tmp_path, "github:acme/pdf", allowlist=[]))
    assert out is None
    assert spy == []  # fast reject — fetch_candidate must NOT be called


def test_allowlisted_but_carries_code_refused(monkeypatch, tmp_path):
    _wire(monkeypatch, verdict="safe", carries_code=True)
    out = asyncio.run(skill_acquire.acquire_safe_seed(
        tmp_path, "github:acme/pdf", allowlist=["github:acme"]))
    assert out is None


def test_unresolvable_source_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.agent.skill_resolve.resolve_candidates",
                        lambda ref: _Resolve([]))
    out = asyncio.run(skill_acquire.acquire_safe_seed(
        tmp_path, "github:acme/pdf", allowlist=["github:acme"]))
    assert out is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/agent/test_skill_acquire.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'durin.agent.skill_acquire'`.

- [ ] **Step 3: Write the minimal implementation**

```python
# durin/agent/skill_acquire.py
"""§6.C acquire-on-gap — gated per-ref seed retrieval.

Given ONE registry ref the dream chose from a raw ``skill_search`` result, run the
§8.C gate and return its SKILL.md body ONLY if ``decide_action == 'allow'`` — else
None ("pick another"). The autonomous risk rule is enforced HERE, in code: the dream
(no human present) can never receive risky content. Cost-aware: a non-allowlisted ref
can never reach 'allow', so it is rejected INSTANTLY without a download; only
allowlisted (user-trusted) refs are fetched, and the gate's STATIC scan runs with the
LLM judge OFF. Path A (in-session, human present) does not use this — it drives the
raw tools and routes risky candidates to the user via ``ask_user_question``.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path


async def acquire_safe_seed(workspace, source: str, *, allowlist) -> dict | None:
    """Gate ONE registry ref for use as a seed. Returns
    ``{"name", "source", "content"}`` when the §8.C gate rates it ``allow``, else
    ``None``. Rejects a non-allowlisted ref without downloading it."""
    from durin.agent.skill_resolve import resolve_candidates
    from durin.agent.skills_import import (
        decide_action, fetch_candidate, validate_skill,
    )
    from durin.security.skill_scan import scan_skill

    allow = [p for p in (allowlist or []) if p]
    source = (source or "").strip()
    if not source:
        return None
    # Fast reject (no network): a non-allowlisted source can never reach 'allow'.
    if not any(source.startswith(p) for p in allow):
        return None

    res = resolve_candidates(source)
    if not res.candidates:
        return None
    cand = res.candidates[0]
    seed_root = Path(workspace) / ".durin" / "acquire-quarantine"
    try:
        qdir = await asyncio.to_thread(
            fetch_candidate, cand, quarantine_root=seed_root,
            allowlist=allow, judge_trigger="off")  # static scan only — never the judge
    except Exception:  # noqa: BLE001 — a bad candidate must not sink the caller
        return None
    try:
        vr = validate_skill(qdir)
        rep = scan_skill(qdir)
        action = decide_action(
            source, verdict=rep.verdict, carries_code=vr.carries_code, allowlist=allow)
        if action == "allow":
            body = (qdir / "SKILL.md").read_text(encoding="utf-8")
            return {"name": cand.name, "source": source, "content": body}
        return None
    finally:
        shutil.rmtree(qdir, ignore_errors=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/agent/test_skill_acquire.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add durin/agent/skill_acquire.py tests/agent/test_skill_acquire.py
git commit -m "feat(skills): §6.C acquire_safe_seed — gated registry seed (safe-only)"
```

---

## Task 2: `skill_acquire_seed` tool wrapper (per-ref)

**Files:**
- Create: `durin/agent/tools/skill_acquire_seed.py`
- Test: `tests/agent/test_skill_acquire_seed_tool.py`

**Pattern note:** mirror the EXACT `Tool` shape of `durin/agent/tools/skill_search.py`
(read it first). It declares parameters with `tool_parameters_schema`, has a `name`
property, a `create(cls, ctx)` classmethod that reads `ctx.app_config.skills` (falling
back to `load_config().skills`), and an async `execute`. Match whatever mechanism
`skill_search.py` uses to attach `_PARAMETERS` (e.g. a `@tool_parameters(...)`
decorator vs a `parameters` property) — do not invent a different one.

The tool takes a single `source` (a ref the dream picked from a `skill_search` hit)
and pulls only the **allowlist** from config (no registries/limit — there is no search
here; search is the separate raw `skill_search` tool).

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_skill_acquire_seed_tool.py
"""§6.C — the skill_acquire_seed tool gates ONE ref via acquire_safe_seed."""
import asyncio

from durin.agent.tools.skill_acquire_seed import SkillAcquireSeedTool


def test_tool_name(tmp_path):
    tool = SkillAcquireSeedTool(workspace=tmp_path, allowlist=[])
    assert tool.name == "skill_acquire_seed"


def test_execute_returns_seed(monkeypatch, tmp_path):
    async def _fake(workspace, source, *, allowlist):
        return {"name": "pdf", "source": source, "content": "body"}

    monkeypatch.setattr("durin.agent.skill_acquire.acquire_safe_seed", _fake)
    tool = SkillAcquireSeedTool(workspace=tmp_path, allowlist=["github:acme"])
    out = asyncio.run(tool.execute(source="github:acme/pdf"))
    assert out["seed"]["source"] == "github:acme/pdf"


def test_execute_no_seed(monkeypatch, tmp_path):
    async def _none(workspace, source, *, allowlist):
        return None

    monkeypatch.setattr("durin.agent.skill_acquire.acquire_safe_seed", _none)
    tool = SkillAcquireSeedTool(workspace=tmp_path, allowlist=[])
    out = asyncio.run(tool.execute(source="github:acme/pdf"))
    assert out["seed"] is None
    assert "note" in out


def test_execute_missing_source(tmp_path):
    tool = SkillAcquireSeedTool(workspace=tmp_path, allowlist=["github:acme"])
    out = asyncio.run(tool.execute(source=""))
    assert out["seed"] is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_skill_acquire_seed_tool.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'durin.agent.tools.skill_acquire_seed'`.

- [ ] **Step 3: Write the minimal implementation**

```python
# durin/agent/tools/skill_acquire_seed.py
"""skill_acquire_seed tool — §6.C. Given ONE registry ref the dream chose from a raw
skill_search hit, return a RISK-FREE seed (gate verdict 'allow') to author from, or
{seed: null} to tell the dream to pick another. The gate runs in code
(acquire_safe_seed), so a risky/un-allowlisted ref is never handed back. Lives in the
dream phase-2 toolset; the in-session agent uses raw skill_search/skill_import/
ask_user_question instead (a human is present to approve risky candidates)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

# NOTE: if skill_search.py attaches parameters via a decorator (e.g.
# `@tool_parameters(_PARAMETERS)`) instead of a `parameters` property, mirror that
# exact mechanism here — keep the schema content below identical.
_PARAMETERS = tool_parameters_schema(
    source=StringSchema(
        "A registry ref from a skill_search hit to evaluate as a seed "
        "(e.g. 'github:owner/repo/skill' or 'clawhub:slug')."),
    required=["source"],
    description=(
        "Given a ref from a skill_search result, return a RISK-FREE seed (SKILL.md "
        "body) to author a new skill from — only if it clears the security gate. "
        "Returns {seed: null} when it needs user consent or can't be fetched; then "
        "pick another hit or author from scratch. Never installs; never returns "
        "risky code."
    ),
)


class SkillAcquireSeedTool(Tool):
    """Return a safe registry seed for one chosen ref, or null (pick another)."""

    def __init__(self, workspace, allowlist):
        self._workspace = Path(workspace)
        self._allowlist = allowlist

    @property
    def name(self) -> str:
        return "skill_acquire_seed"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @property
    def parameters(self) -> dict:
        return _PARAMETERS

    @classmethod
    def create(cls, ctx: Any) -> "SkillAcquireSeedTool":
        allowlist: list[str] = []
        try:
            sk = ctx.app_config.skills
        except Exception:  # noqa: BLE001
            try:
                from durin.config.loader import load_config
                sk = load_config().skills
            except Exception:  # noqa: BLE001
                sk = None
        if sk is not None:
            allowlist = list(sk.security.allowlist)
        return cls(workspace=ctx.workspace, allowlist=allowlist)

    async def execute(self, **kwargs: Any) -> Any:
        from durin.agent.skill_acquire import acquire_safe_seed

        source = str(kwargs.get("source", "")).strip()
        if not source:
            return {"seed": None, "note": "source is required"}
        seed = await acquire_safe_seed(
            self._workspace, source, allowlist=self._allowlist)
        if seed is None:
            return {"seed": None,
                    "note": "needs user consent or unfetchable — pick another hit "
                            "or author from scratch"}
        return {"seed": seed,
                "note": "adapt this seed; it passed the security gate"}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_skill_acquire_seed_tool.py -q`
Expected: PASS (4 passed).

> If construction/`name`/parameters shape differs from the real `Tool` base, open
> `durin/agent/tools/skill_search.py` + `durin/agent/tools/base.py` and match that
> class exactly. Keep the test intent: `SkillAcquireSeedTool(workspace=..., allowlist=...)`
> constructs, `.name == "skill_acquire_seed"`, `execute(source=...)` returns `{"seed": ...}`.

- [ ] **Step 5: Commit**

```bash
git add durin/agent/tools/skill_acquire_seed.py tests/agent/test_skill_acquire_seed_tool.py
git commit -m "feat(skills): §6.C skill_acquire_seed tool (per-ref, gated)"
```

---

## Task 3: Restrict the seed tool to dream-only + register both tools in the dream

**Why:** `SkillAcquireSeedTool` has no `_scopes`, so the main-loop auto-loader would
put it in core — but Path A (in-session) must use the RAW `skill_search` /
`skill_import` / `ask_user_question` (a human approves risky candidates), NOT the
gated tool that silently skips them. So mark the seed tool `dream`-scoped (excluded
from core auto-load) and register it — plus the raw `skill_search` — manually in the
dream's own toolset.

**Files:**
- Modify: `durin/agent/tools/skill_acquire_seed.py` (add `_scopes`)
- Modify: `durin/agent/memory.py` (`Dream._build_tools`, after the `SkillWriteTool` registration)
- Test: `tests/agent/test_skill_acquire_seed_tool.py` (append registration + scope tests)

- [ ] **Step 1: Write the failing tests (append to the tool test file)**

```python
def test_acquire_seed_excluded_from_core_autoload():
    # Path A uses raw tools; the gated seed tool must not auto-load into the main loop.
    assert "core" not in getattr(SkillAcquireSeedTool, "_scopes", {"core"})


def test_dream_toolset_has_search_and_seed_and_write(tmp_path):
    from durin.agent.memory import Dream, MemoryStore

    class _Prov:  # minimal provider stand-in; _build_tools never calls it
        pass

    store = MemoryStore(workspace=tmp_path)
    dream = Dream(store=store, provider=_Prov(), model="x")
    names = {t.name for t in dream._tools.all()}
    assert "skill_search" in names        # raw search — dream sees the full hit list
    assert "skill_acquire_seed" in names  # gated per-ref retrieval
    assert "skill_write" in names         # regression: existing authoring tool
```

> If `MemoryStore(workspace=...)`, `Dream(...)`, or `dream._tools.all()` doesn't match
> the real API, read `durin/agent/memory.py` around `class Dream` / `class MemoryStore`
> and the `ToolRegistry` class, and adjust construction + enumeration (e.g.
> `dream._tools.names()`), keeping the assertion intent.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_skill_acquire_seed_tool.py -q`
Expected: FAIL — no `_scopes` yet; `skill_search`/`skill_acquire_seed` not in the dream toolset.

- [ ] **Step 3a: Add `_scopes` to the seed tool**

In `durin/agent/tools/skill_acquire_seed.py`, add this class attribute at the top of
the `SkillAcquireSeedTool` class body (just under the class docstring):

```python
    # Dream-only: Path A (in-session) uses the raw skill_search/skill_import/
    # ask_user_question tools so the user approves risky candidates. This gated tool
    # silently skips risky ones, which is correct ONLY for the autonomous dream.
    _scopes = {"dream"}
```

- [ ] **Step 3b: Register both tools in `Dream._build_tools`**

In `durin/agent/memory.py`, immediately after the existing
`tools.register(SkillWriteTool(workspace=workspace))` line, add:

```python
        # §6.C: the dream sees the full hit list (raw skill_search) and asks a gated
        # per-ref tool for a seed; the gate (in skill_acquire_seed) only ever returns
        # risk-free prior art, so the autonomous dream can never use risky content.
        from durin.agent.tools.skill_acquire_seed import SkillAcquireSeedTool
        from durin.agent.tools.skill_search import SkillSearchTool
        from durin.config.loader import load_config
        try:
            _sk = load_config().skills
            _regs = list(_sk.discovery.registries)
            _allow = list(_sk.security.allowlist)
            _lim = int(_sk.discovery.search_limit)
        except Exception:  # noqa: BLE001 — never block dream startup on config
            _regs, _allow, _lim = [], [], 10
        tools.register(SkillSearchTool(
            workspace=workspace, registries=_regs, allowlist=_allow, limit=_lim))
        tools.register(SkillAcquireSeedTool(workspace=workspace, allowlist=_allow))
```

> Verify `SkillSearchTool.__init__` signature in `durin/agent/tools/skill_search.py`
> (it is `(self, workspace, registries, allowlist, limit)`); match it. If different,
> adjust the construction to match the real signature.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_skill_acquire_seed_tool.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Run the dream + skill test modules to check for regressions**

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/ -k "dream or skill" -q`
Expected: PASS (no regressions).

- [ ] **Step 6: Commit**

```bash
git add durin/agent/tools/skill_acquire_seed.py durin/agent/memory.py tests/agent/test_skill_acquire_seed_tool.py
git commit -m "feat(skills): §6.C dream phase-2 gets raw skill_search + gated skill_acquire_seed (dream-scoped)"
```

---

## Task 4: Path B — instruct phase-2 to seed before authoring

**Files:**
- Modify: `durin/templates/agent/dream_phase2.md` (the "Skill creation rules" section, ~line 22)

- [ ] **Step 1: Read the current skill-creation block**

Run: `sed -n '1,35p' durin/templates/agent/dream_phase2.md`
Confirm the `## Skill creation rules (for [SKILL] entries)` heading and the
`skill_write(name, content, rationale)` line exist.

- [ ] **Step 2: Add the seed step (no test — prompt; verified live in Task 6)**

Under `## Skill creation rules (for [SKILL] entries)`, immediately before the line
that documents `skill_write(...)`, insert:

```markdown
- **Before authoring, look for prior art (registries):** use `skill_search` with a
  short phrase for the capability to get candidate hits. For a relevant hit, call
  `skill_acquire_seed(source=<the hit's ref>)`. If it returns a `seed`, ADAPT that
  body (fix names/paths, drop irrelevant parts) and pass the result as `skill_write`'s
  `content` — do not copy it verbatim. If it returns `{seed: null}` (needs consent or
  unfetchable), try another hit from the search results, or author from scratch. You
  never need to judge a candidate's safety — `skill_acquire_seed` only ever hands back
  risk-free prior art.
```

- [ ] **Step 3: Verify the template still renders**

Run:
```bash
.venv/bin/python -c "from durin.agent.context import render_template; print(render_template('agent/dream_phase2.md')[:200])"
```
Expected: prints the first 200 chars without a template error.

> If `render_template` requires variables for this template, render it the way the
> dream does — grep `render_template(.*dream_phase2` in `durin/agent/memory.py` and
> mirror that call (it may need no args, or `skill_creator_path=...`).

- [ ] **Step 4: Commit**

```bash
git add durin/templates/agent/dream_phase2.md
git commit -m "feat(skills): §6.C dream phase-2 seeds from prior art before authoring"
```

---

## Task 5: Path A — in-session acquire-on-gap prompt

**Files:**
- Modify: `durin/templates/agent/skills_section.md`

- [ ] **Step 1: Read the current template**

Run: `cat durin/templates/agent/skills_section.md`
Confirm the existing line about `memory_search` with `kind="skill"` before
concluding no skill exists.

- [ ] **Step 2: Extend the search guidance (no test — prompt; verified live in Task 6)**

Append, immediately after the existing `memory_search`/`kind="skill"` paragraph,
this paragraph:

```markdown
If local skill search still finds nothing and the task is a **recurring or
non-trivial workflow** (the kind you'd want to not reinvent next time), search the
external registries with `skill_search` before reinventing it. To reuse a hit, fetch
it with `skill_import(action="fetch", source=<ref>)` — that runs the security gate.
If the gate clears it, adapt it into a new skill with `skill_write`. If the gate
flags it (carries code, caution, or an un-allowlisted source), do **not** install it
silently: present the candidates to the user with `ask_user_question` (recommended
one first; say which need extra tools installed) and let them decide.
```

- [ ] **Step 3: Verify the template renders**

Run:
```bash
.venv/bin/python -c "from durin.agent.context import render_template; print(render_template('agent/skills_section.md', skills_summary='- x')[:300])"
```
Expected: prints the rendered block including the new paragraph, no template error.

- [ ] **Step 4: Commit**

```bash
git add durin/templates/agent/skills_section.md
git commit -m "feat(skills): §6.C in-session acquire-on-gap prompt (search→gate→author)"
```

---

## Task 6: Live verification (unit-green ≠ working feature)

**No new files. Verify against the real binary + real registries + real model.**

- [ ] **Step 1: Full suite green**

Run: `.venv/bin/python -m pytest tests/agent/test_skill_acquire.py tests/agent/test_skill_acquire_seed_tool.py -q`
Expected: PASS (all).

- [ ] **Step 2: Path B live — dream seeds from a real registry**

Set a real allowlisted source in the workspace config (otherwise every hit →
`confirm` → no seed, by design). Add a trusted owner to
`skills.security.allowlist` (e.g. `"github:anthropics"`). Then run the dream once
against a workspace whose history contains a recurring workflow that maps to a real
skills.sh/clawhub skill, and confirm in the logs that `skill_acquire_seed` returned a
seed and `skill_write` authored from it.

Run:
```bash
.venv/bin/python -c "import asyncio; from durin.agent.skill_acquire import acquire_safe_seed; print(asyncio.run(acquire_safe_seed('.', 'extract tables from pdf', registries=__import__('durin.config.loader', fromlist=['load_config']).load_config().skills.discovery.registries, allowlist=['github:anthropics'], limit=5)))"
```
Expected: prints a `{"name":..., "source":..., "content":...}` dict for an
allowlisted hit, or `None` if no allowlisted hit matches (try a query/owner that
exists on the registry). Confirms the real network + fetch + gate path end-to-end.

- [ ] **Step 3: Path A live — in-session acquire**

Launch the real agent (`.venv/bin/python -m durin ...` per the project's run skill),
give it a task needing a capability with no local skill, and confirm it (a) searches
local skills, (b) calls `skill_search`, (c) on a safe hit authors via `skill_write`,
(d) on a risky hit asks via `ask_user_question`. Capture the transcript.

- [ ] **Step 4: Record the result**

Update the spec status block (or `docs/archive/skills_evolutivas.md`) to mark §6.C
BUILT with a one-line note of what was verified live. Commit.

```bash
git add docs/
git commit -m "docs(skills): §6.C built + live-verified (Path A in-session, Path B dream seed)"
```

---

## Self-Review

**Spec coverage:**
- §4 Path A (in-session, prompt-driven, risk→ask_user) → Task 5. ✓
- §4 Path B (dream phase-2, autonomous, safe-only) → Tasks 1–4. ✓
- §4 risk rule (`decide_action == allow` only; risky → user / quarantine) → enforced
  in `acquire_safe_seed` (Task 1) for Path B; routed to `ask_user_question` (Task 5)
  for Path A. ✓
- §5.1 (Path B mechanism) → refined to a gated tool (noted at top). ✓
- §5.2 (`ask_user_question`) → Task 5. ✓
- §6 components (prompt nudge, seed hook, gate wiring) → Tasks 1–5. ✓
- §7 testing (gate logic unit-tested; prompt verified live) → Tasks 1–3 + Task 6. ✓
- §8 out-of-scope (§6.D adaptation, system tier) → not touched. ✓

**Placeholder scan:** no TBD/TODO; every code step shows complete code; the two
prompt tasks show the exact text to insert. Fallback notes (Task 2/3/4) point at the
real file to mirror if a base-class/template API differs — these are guardrails, not
placeholders.

**Type consistency:** `acquire_safe_seed(workspace, query, *, registries, allowlist,
limit)` returns `{"name","source","content"} | None`, used identically by the tool
(Task 2) and live check (Task 6). The tool returns `{"seed": <that-or-None>, "note"}`,
matched by its tests. `decide_action(source, *, verdict, carries_code, allowlist)` and
`fetch_candidate(cand, *, quarantine_root, allowlist=...)` match the real signatures
in `durin/agent/skills_import.py`.
