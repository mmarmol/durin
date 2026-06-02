# Skills Evolution E2 — Part A Implementation Plan

> ✅ **EJECUTADO** (merged a main, release local `v0.1.0a9`). Plan cerrado. El
> diseño y estado vivos están en el spec de E2.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture skill-usage signal (`skill_calls`) in each session's durable `.meta.json`, and route the 2h dream's skill authoring through E1's `skills_store` (provenance + commit + fork-on-write), removing the raw `WriteFileTool` path.

**Architecture:** Two halves. (1) **Signal** — a pure extractor scans each turn's messages for skill operations (`read_file` on a `SKILL.md`, `skill_edit`) and the loop appends them to `session.metadata["skill_calls"]`, which persists to the sidecar `.meta.json` via `_DERIVED_METADATA_KEYS`. (2) **Authoring via E1** — new `skills_store` helpers stamp `provenance.source="dream"`, a Dream-only `SkillWriteTool` wraps them, and `Dream._build_tools` swaps `WriteFileTool` for it. A final task lets the dream read recent `skill_calls` to drive patches.

**Tech Stack:** Python 3.12, pytest (`tmp_path`), durin's `skills_store` (frontmatter via `split_frontmatter`/`join_frontmatter`/`ensure_durin`), `GitStore` (skills subtree).

**Spec:** [`docs/superpowers/specs/2026-06-02-skills-evolution-e2-design.md`](../specs/2026-06-02-skills-evolution-e2-design.md) (§4 señal, §5 Parte A, §7 reuso E1).

**Verified facts this plan relies on (Apéndice A of the spec):**
- `read_file` emits and its tool-call carries the `path`; skills are loaded on-demand by `read_file` on `skills/<name>/SKILL.md`.
- `_state_save` (loop.py) already mutates `ctx.session.metadata` and then `self.sessions.save(ctx.session)`.
- `_DERIVED_METADATA_KEYS = frozenset({"_last_summary", "_last_tags"})` (session/manager.py) — split/merged to/from `.meta.json` automatically.
- `skills_store` helpers: `split_frontmatter`, `join_frontmatter`, `ensure_durin`, `_update_md`, `_store_init`, `_safe_name`, `_today`, `_skill_md`, `_skills_dir`. `apply_skill_edit(workspace, name, *, old, new, rationale, ...)` already does fork-on-write + mode gate + commit.
- `Dream._build_tools` (memory.py ~1136-1160) registers `WriteFileTool(allowed_dir=skills_dir)`.

---

## Task 1: `skill_calls` extractor (pure function)

**Files:**
- Create: `durin/agent/skill_usage.py`
- Test: `tests/agent/test_skill_usage.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_skill_usage.py
from durin.agent.skill_usage import extract_skill_calls


def test_read_file_on_skill_md_is_a_read_call():
    messages = [
        {"role": "assistant", "tool_calls": [
            {"id": "1", "type": "function", "function": {
                "name": "read_file",
                "arguments": '{"path": "skills/git-helper/SKILL.md"}',
            }},
        ]},
    ]
    assert extract_skill_calls(messages) == [{"skill": "git-helper", "op": "read"}]


def test_skill_edit_is_an_edit_call():
    messages = [
        {"role": "assistant", "tool_calls": [
            {"id": "2", "type": "function", "function": {
                "name": "skill_edit",
                "arguments": {"name": "deploy-flow", "old": "a", "new": "b"},
            }},
        ]},
    ]
    assert extract_skill_calls(messages) == [{"skill": "deploy-flow", "op": "edit"}]


def test_non_skill_read_and_other_tools_are_ignored():
    messages = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "read_file", "arguments": '{"path": "src/main.py"}'}},
            {"function": {"name": "grep", "arguments": '{"pattern": "x"}'}},
        ]},
        {"role": "user", "content": "hi"},
    ]
    assert extract_skill_calls(messages) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_skill_usage.py -v`
Expected: FAIL with `ModuleNotFoundError: durin.agent.skill_usage`

- [ ] **Step 3: Write minimal implementation**

```python
# durin/agent/skill_usage.py
"""Extract skill-usage signal (`skill_calls`) from a turn's messages.

A skill "call" is the agent touching a skill during a turn:
- ``read``  — ``read_file`` on ``skills/<name>/SKILL.md`` (progressive load).
- ``edit``  — ``skill_edit`` on a skill (E1 editor).

Pure and dependency-free so it's trivially unit-testable and safe to run in the
hot loop. The result is appended to ``session.metadata["skill_calls"]`` and
persists to the durable ``.meta.json`` sidecar.
"""
from __future__ import annotations

import json
import re
from typing import Any

# skills/<name>/SKILL.md  (workspace or builtin path forms both end this way)
_SKILL_PATH_RE = re.compile(r"(?:^|/)skills/([^/]+)/SKILL\.md$")


def _tool_name_and_args(tc: Any) -> tuple[str, dict]:
    """Normalize a tool-call dict to (name, args-dict). Tolerates the nested
    ``{"function": {"name", "arguments"}}`` shape and a flat ``{"name",
    "arguments"}`` shape; ``arguments`` may be a JSON string or a dict."""
    fn = tc.get("function") if isinstance(tc, dict) else None
    src = fn if isinstance(fn, dict) else tc
    name = src.get("name", "") if isinstance(src, dict) else ""
    raw = src.get("arguments", {}) if isinstance(src, dict) else {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    return name, raw if isinstance(raw, dict) else {}


def extract_skill_calls(messages: list[dict]) -> list[dict]:
    calls: list[dict] = []
    for message in messages:
        for tc in (message.get("tool_calls") or []):
            name, args = _tool_name_and_args(tc)
            if name == "read_file":
                m = _SKILL_PATH_RE.search(str(args.get("path", "")))
                if m:
                    calls.append({"skill": m.group(1), "op": "read"})
            elif name == "skill_edit":
                skill = args.get("name")
                if skill:
                    calls.append({"skill": skill, "op": "edit"})
    return calls
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_skill_usage.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add durin/agent/skill_usage.py tests/agent/test_skill_usage.py
git commit -m "feat(skills): pure extractor for skill_calls usage signal"
```

---

## Task 2: Persist `skill_calls` to the `.meta.json` sidecar

**Files:**
- Modify: `durin/session/manager.py` (the `_DERIVED_METADATA_KEYS` frozenset)
- Test: `tests/agent/test_skill_calls_persist.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_skill_calls_persist.py
from durin.session.manager import SessionManager


def test_skill_calls_round_trip_through_meta_sidecar(tmp_path):
    sm = SessionManager(tmp_path)
    s = sm.get_or_create("websocket:abc")
    s.add_message("user", "do a thing")
    s.metadata["skill_calls"] = [{"skill": "git-helper", "op": "read"}]
    sm.save(s)

    # Drop the in-memory cache so load reads from disk (line-0 + .meta.json).
    sm._cache.clear()
    reloaded = sm.get_or_create("websocket:abc")
    assert reloaded.metadata.get("skill_calls") == [
        {"skill": "git-helper", "op": "read"}
    ]


def test_skill_calls_live_in_the_meta_sidecar_not_line_0(tmp_path):
    import json
    from durin.session.paths import meta_path_for  # re-exported in manager imports

    sm = SessionManager(tmp_path)
    s = sm.get_or_create("websocket:abc")
    s.add_message("user", "x")
    s.metadata["skill_calls"] = [{"skill": "a", "op": "edit"}]
    sm.save(s)

    meta = json.loads(meta_path_for("websocket:abc", sm.sessions_dir).read_text())
    assert meta["derived"]["skill_calls"] == [{"skill": "a", "op": "edit"}]
```

> Note: import `meta_path_for` from the module `manager.py` already imports it
> from (see its top-of-file `from ... import meta_path_for, read_derived,
> write_derived`). Adjust the import path in the test to match that module.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_skill_calls_persist.py -v`
Expected: FAIL — `skill_calls` flows through line-0, not the `derived` sidecar (second test KeyErrors / first test may pass by accident via line-0; the sidecar assertion fails).

- [ ] **Step 3: Add the key to the derived registry**

In `durin/session/manager.py`, extend the frozenset:

```python
    _DERIVED_METADATA_KEYS = frozenset({"_last_summary", "_last_tags", "skill_calls"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_skill_calls_persist.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add durin/session/manager.py tests/agent/test_skill_calls_persist.py
git commit -m "feat(skills): persist skill_calls in the durable .meta.json sidecar"
```

---

## Task 3: Record `skill_calls` from the loop (post-turn)

**Files:**
- Modify: `durin/agent/loop.py` (`_state_save`)
- Test: `tests/agent/test_loop_skill_calls.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_loop_skill_calls.py
from durin.agent.skill_usage import extract_skill_calls


def _record_skill_calls(session_metadata: dict, messages: list[dict]) -> None:
    # Mirror of the helper the loop will call; tested here in isolation so we
    # don't have to stand up a full AgentLoop.
    calls = extract_skill_calls(messages)
    if calls:
        session_metadata.setdefault("skill_calls", []).extend(calls)


def test_recording_appends_to_existing_skill_calls():
    md = {"skill_calls": [{"skill": "old", "op": "read"}]}
    msgs = [{"role": "assistant", "tool_calls": [
        {"function": {"name": "skill_edit", "arguments": {"name": "git-helper"}}},
    ]}]
    _record_skill_calls(md, msgs)
    assert md["skill_calls"] == [
        {"skill": "old", "op": "read"},
        {"skill": "git-helper", "op": "edit"},
    ]


def test_recording_is_noop_when_no_skill_calls():
    md = {}
    _record_skill_calls(md, [{"role": "user", "content": "hi"}])
    assert "skill_calls" not in md
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_loop_skill_calls.py -v`
Expected: PASS for the isolated helper — BUT this task's real change is wiring it into `_state_save`. Keep this test (it pins the contract); the wiring is verified by the existing loop tests not breaking. If the import line is wrong it fails at import.

- [ ] **Step 3: Wire into `_state_save`**

In `durin/agent/loop.py`, add the import near the other agent-mode import inside `_state_save` (or at module top with the other `durin.agent` imports):

```python
from durin.agent.skill_usage import extract_skill_calls
```

Then in `_state_save`, immediately after the existing
`clear_executing_plan_if_todos_done(ctx.session.metadata)` line and before
`self._save_turn(...)`, add:

```python
        # E2 Part A: durable skill-usage signal. Scan this turn's messages for
        # skill activations/edits and append to the session's derived metadata
        # (persists to <key>.meta.json — never capped, survives consolidation).
        _skill_calls = extract_skill_calls(ctx.all_messages)
        if _skill_calls:
            ctx.session.metadata.setdefault("skill_calls", []).extend(_skill_calls)
```

- [ ] **Step 4: Run the full agent loop + session tests**

Run: `pytest tests/agent/test_loop_skill_calls.py tests/agent/test_session_manager_history.py -v`
Expected: PASS (no regressions; new contract test green)

- [ ] **Step 5: Commit**

```bash
git add durin/agent/loop.py tests/agent/test_loop_skill_calls.py
git commit -m "feat(skills): record skill_calls into session metadata post-turn"
```

---

## Task 4: `skills_store` dream-authoring helpers (`source=dream`)

**Files:**
- Modify: `durin/agent/skills_store.py`
- Test: `tests/agent/test_skills_store_dream.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_skills_store_dream.py
from durin.agent import skills_store as ss
from durin.agent.skills_frontmatter import split_frontmatter


def test_dream_create_skill_stamps_provenance_and_commits(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    res = ss.dream_create_skill(
        ws, "deploy-flow", "# Deploy\n\nSteps...\n", rationale="recurring deploy"
    )
    assert res.get("ok") is True
    assert res.get("commit")  # a sha was returned

    text = (ws / "skills" / "deploy-flow" / "SKILL.md").read_text()
    data, body = split_frontmatter(text)
    durin = data["metadata"]["durin"]
    assert durin["mode"] == "auto"
    assert durin["provenance"]["source"] == "dream"
    assert "Deploy" in body


def test_dream_create_rejects_existing_skill(tmp_path):
    ws = tmp_path / "ws"
    (ws / "skills" / "x").mkdir(parents=True)
    (ws / "skills" / "x" / "SKILL.md").write_text("---\nname: x\n---\nbody\n")
    res = ss.dream_create_skill(ws, "x", "new", rationale="r")
    assert "error" in res


def test_dream_create_rejects_unsafe_name(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    res = ss.dream_create_skill(ws, "../escape", "c", rationale="r")
    assert "error" in res
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_skills_store_dream.py -v`
Expected: FAIL — `module 'skills_store' has no attribute 'dream_create_skill'`

- [ ] **Step 3: Add the helper**

In `durin/agent/skills_store.py`, after `save_skill_content` (keeps authoring
helpers together), add:

```python
def dream_create_skill(workspace: Path, name: str, content: str,
                       rationale: str) -> dict:
    """Create a NEW skill authored by the dream: stamp mode=auto +
    provenance.source='dream', write SKILL.md, commit. Refuses to overwrite
    an existing skill (that path is an edit, not a create)."""
    if not _safe_name(name):
        return {"error": "invalid skill name"}
    if not rationale or not rationale.strip():
        return {"error": "rationale is required"}
    md = _skill_md(workspace, name)
    if md.exists():
        return {"error": f"skill already exists: {name}"}
    store = _store_init(workspace)  # ensure git repo exists before mutating files
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(content, encoding="utf-8")

    def _stamp(data: dict) -> None:
        durin = ensure_durin(data)
        durin["mode"] = "auto"
        durin["provenance"] = {"source": "dream", "created_at": _today()}

    _update_md(md, _stamp)
    sha = store.auto_commit(f"skill({name}): {rationale.strip()} [dream]")
    return {"ok": True, "name": name, "commit": sha}
```

> Patching an existing `auto` skill reuses the already-shipped
> `apply_skill_edit(...)` (fork-on-write + mode gate + commit). No new edit
> helper is needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_skills_store_dream.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add durin/agent/skills_store.py tests/agent/test_skills_store_dream.py
git commit -m "feat(skills): skills_store.dream_create_skill (source=dream, commit)"
```

---

## Task 5: `SkillWriteTool` — the dream's sanctioned authoring tool

**Files:**
- Create: `durin/agent/tools/skill_write.py`
- Test: `tests/agent/test_skill_write_tool.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_skill_write_tool.py
import asyncio

from durin.agent.tools.skill_write import SkillWriteTool
from durin.agent.skills_store import read_mode


def test_skill_write_tool_creates_via_skills_store(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = SkillWriteTool(workspace=ws)
    out = asyncio.run(tool.execute(
        name="git-helper", content="# Git helper\n\nuse rebase\n",
        rationale="recurring git flow",
    ))
    assert "git-helper" in out
    assert (ws / "skills" / "git-helper" / "SKILL.md").exists()
    assert read_mode(ws, "git-helper") == "auto"


def test_skill_write_tool_reports_errors(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = SkillWriteTool(workspace=ws)
    out = asyncio.run(tool.execute(name="../bad", content="x", rationale="r"))
    assert "error" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_skill_write_tool.py -v`
Expected: FAIL — `ModuleNotFoundError: durin.agent.tools.skill_write`

- [ ] **Step 3: Implement the tool**

Mirror the shape of `durin/agent/tools/skill_edit.py` (the E1 tool) for the
base-class, schema, and `create(ctx)` conventions. Minimal version:

```python
# durin/agent/tools/skill_write.py
"""Dream-only tool: author a NEW skill through skills_store (source=dream).

Replaces the raw WriteFileTool the 2h dream used to dump SKILL.md with — this
goes through E1's service layer so every dream-authored skill is a first-class
citizen (provenance, mode=auto, committed to the skills subtree)."""
from __future__ import annotations

import json
from pathlib import Path

from durin.agent.skills_store import dream_create_skill
from durin.agent.tools.base import Tool
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

_PARAMS = tool_parameters_schema(
    name=StringSchema("Skill name (directory under skills/). No slashes."),
    content=StringSchema("Full SKILL.md markdown body (frontmatter optional)."),
    rationale=StringSchema("Why this skill is being created (committed message)."),
)


class SkillWriteTool(Tool):
    name = "skill_write"
    description = (
        "Create a new skill. Writes skills/<name>/SKILL.md through the sanctioned "
        "store (provenance source=dream, mode=auto, committed). Use for a recurring "
        "pattern that no existing skill covers."
    )
    parameters = _PARAMS

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)

    @classmethod
    def create(cls, ctx):  # ctx.app_config-style wiring, mirrors skill_edit.py
        return cls(workspace=Path(ctx.workspace))

    async def execute(self, name: str, content: str, rationale: str) -> str:
        res = dream_create_skill(self.workspace, name, content, rationale)
        return json.dumps(res, ensure_ascii=False)
```

> Match the exact base-class / `create(ctx)` signature and schema imports to
> `skill_edit.py` in this repo; the snippet above is the intent, adjust the
> imports/registration to the real `Tool` API if they differ.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_skill_write_tool.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add durin/agent/tools/skill_write.py tests/agent/test_skill_write_tool.py
git commit -m "feat(skills): SkillWriteTool — dream authors via skills_store"
```

---

## Task 6: Swap `WriteFileTool` → `SkillWriteTool` in the dream

**Files:**
- Modify: `durin/agent/memory.py` (`Dream._build_tools`)
- Modify: `durin/templates/agent/dream_phase2.md` (prompt: use `skill_write`, not `write_file`)
- Test: `tests/agent/test_dream_tools.py` (extend the existing file)

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_dream_tools.py  (add this test)
def test_dream_registers_skill_write_not_write_file(tmp_path):
    from durin.agent.memory import Dream, MemoryStore

    store = MemoryStore(tmp_path)
    dream = Dream(store=store)
    tools = dream._build_tools()
    names = set(tools.names()) if hasattr(tools, "names") else set(tools._tools)
    assert "skill_write" in names
    assert "write_file" not in names
```

> Adjust `Dream(...)` construction and the tool-registry introspection
> (`tools.names()` / iterating the registry) to the real constructor/registry
> API used elsewhere in `test_dream_tools.py`.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_dream_tools.py::test_dream_registers_skill_write_not_write_file -v`
Expected: FAIL — `write_file` present, `skill_write` absent.

- [ ] **Step 3: Edit `_build_tools`**

In `durin/agent/memory.py`, in `Dream._build_tools`, replace the
`WriteFileTool` registration block:

```python
        # write_file resolves relative paths from workspace root, but can only
        # write under skills/ so the prompt can safely use skills/<name>/SKILL.md.
        skills_dir = workspace / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        tools.register(WriteFileTool(workspace=workspace, allowed_dir=skills_dir, file_states=file_states))
        return tools
```

with:

```python
        # E2 Part A: author skills through the sanctioned store (provenance +
        # commit + fork-on-write), not a raw file write.
        from durin.agent.tools.skill_write import SkillWriteTool
        (workspace / "skills").mkdir(parents=True, exist_ok=True)
        tools.register(SkillWriteTool(workspace=workspace))
        return tools
```

Remove the now-unused `WriteFileTool` from the import line:
`from durin.agent.tools.filesystem import EditFileTool, ReadFileTool` (drop
`WriteFileTool`).

- [ ] **Step 4: Update the Phase 2 prompt**

In `durin/templates/agent/dream_phase2.md`, replace any instruction to write
the skill with `write_file` so it instructs the model to call `skill_write`
with `name`, `content`, `rationale`. (Grep the template for `write_file` and
`SKILL.md` and update the wording; do not leave both tools referenced.)

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/agent/test_dream_tools.py -v`
Expected: PASS (existing + new test green)

- [ ] **Step 6: Commit**

```bash
git add durin/agent/memory.py durin/templates/agent/dream_phase2.md tests/agent/test_dream_tools.py
git commit -m "refactor(skills): dream authors via SkillWriteTool, drop raw write_file"
```

---

## Task 7: Dream reads recent `skill_calls` (patch context)

**Files:**
- Modify: `durin/agent/skill_usage.py` (add a collector)
- Modify: `durin/agent/memory.py` (`Dream.run` Phase 1 context)
- Test: `tests/agent/test_skill_usage.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# tests/agent/test_skill_usage.py  (add this test)
import json
from durin.agent.skill_usage import collect_recent_skill_calls
from durin.session.manager import SessionManager


def test_collect_recent_skill_calls_aggregates_across_sessions(tmp_path):
    sm = SessionManager(tmp_path)
    for key, skill in (("websocket:a", "git-helper"), ("websocket:b", "git-helper")):
        s = sm.get_or_create(key)
        s.add_message("user", "x")
        s.metadata["skill_calls"] = [{"skill": skill, "op": "read"}]
        sm.save(s)

    agg = collect_recent_skill_calls(tmp_path)
    # git-helper was read in two sessions
    assert agg.get("git-helper", {}).get("read") == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/agent/test_skill_usage.py::test_collect_recent_skill_calls_aggregates_across_sessions -v`
Expected: FAIL — `collect_recent_skill_calls` undefined.

- [ ] **Step 3: Add the collector**

In `durin/agent/skill_usage.py`, append:

```python
def collect_recent_skill_calls(workspace) -> dict[str, dict[str, int]]:
    """Aggregate skill_calls across session sidecars: {skill: {op: count}}.

    Reads the durable ``derived.skill_calls`` of every session's ``.meta.json``.
    Used by the 2h dream to know which `auto` skills were used (candidates to
    patch). A future per-skill cursor (Part B) bounds this by 'since last';
    Part A reads all present sidecars.
    """
    from pathlib import Path
    from durin.session.paths import meta_path_for, read_derived

    workspace = Path(workspace)
    sessions_dir = workspace / "sessions"
    agg: dict[str, dict[str, int]] = {}
    if not sessions_dir.is_dir():
        return agg
    for meta in sessions_dir.glob("*.meta.json"):
        try:
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

> Confirm the real import path/signature of `meta_path_for` / `read_derived`
> (the module `session/manager.py` imports them from). If `read_derived`
> expects a path produced by `meta_path_for(key, sessions_dir)`, derive the key
> from the filename stem instead of globbing — adjust to the real API.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/agent/test_skill_usage.py -v`
Expected: PASS (all green)

- [ ] **Step 5: Surface it in the dream's Phase 1 context**

In `durin/agent/memory.py` `Dream.run`, where the Phase 1 prompt/context is
assembled (alongside `_list_existing_skills`), include a compact
"recently-used auto skills" line built from
`collect_recent_skill_calls(self.store.workspace)` so Phase 2 can choose to
patch a used `auto` skill via `skill_edit`. Keep it to names + counts (cheap).

```python
        from durin.agent.skill_usage import collect_recent_skill_calls
        used = collect_recent_skill_calls(self.store.workspace)
        used_line = ", ".join(
            f"{name} (read×{ops.get('read', 0)}, edit×{ops.get('edit', 0)})"
            for name, ops in sorted(used.items())
        )
        # include `used_line` in the Phase 1 user content when non-empty
```

- [ ] **Step 6: Run the dream tests**

Run: `pytest tests/agent/test_dream.py tests/agent/test_skill_usage.py -v`
Expected: PASS (no regressions)

- [ ] **Step 7: Commit**

```bash
git add durin/agent/skill_usage.py durin/agent/memory.py tests/agent/test_skill_usage.py
git commit -m "feat(skills): dream reads recent skill_calls as patch candidates"
```

---

## Self-review notes (for the implementer)

- **Spec coverage:** Task 1-3 = §4 señal (`skill_calls` in `.meta.json`).
  Task 4-6 = §5/§7 authoring via E1 + remove raw write. Task 7 = §5 patch
  half (read signal). Cursor (per-skill) and global curation are **Part B** —
  out of scope here.
- **Adjust-to-real-API flags:** Tasks 5, 6, 7 carry explicit notes where the
  exact `Tool` base-class signature, `ToolRegistry` introspection, and
  `read_derived`/`meta_path_for` API must be matched to the repo — verify these
  against the real symbols before finalizing each task (do not guess).
- **manual is read-only:** `dream_create_skill` only creates (refuses existing);
  patching uses `apply_skill_edit`, which already gates `mode=manual` (returns
  `proposed` without writing). The dream never overrides a manual skill.
- **No new write path bypasses E1:** after Task 6 the dream has no `write_file`;
  the only skill-writing tool it holds is `skill_write` (→ `skills_store`).
```
