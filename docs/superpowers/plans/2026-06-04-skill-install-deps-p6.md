# P6 #1 — Approved install-spec executor (+ §5.2 git-original prompt) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let durin install a skill's declared dependencies on demand, but only after explicit user approval — closing the gap where a skill is "unavailable" (missing CLI) and the user has to install deps by hand. Plus make it explicit to the dream LLM that a skill's original lives in git (§5.2).

**Architecture:** Today `declared_install_specs` parses + `validate_install_specs` scans install specs, and they are surfaced per skill, but durin never runs them (policy `never`). This adds (a) a helper that turns *safe* install specs into concrete shell commands, and (b) an agent tool `skill_install_deps(name, confirm)`. Grounded in the reference agents: **hermes** routes every command (installs included) through its single command-approval gate (`tools/approval.py`); durin's equivalent is **`ExecTool`** (`_guard_command` allow/deny patterns + sandbox + workspace boundary + logging). So the tool does NOT open a second `subprocess` path — it **runs each confirmed command through `ExecTool`**, reusing durin's one execution gate. `ExecTool`'s gate is policy (patterns), not interactive, so the **explicit user approval** the backlog wants is supplied by durin's dry-run→confirm convention (like `skill_edit`/import): the default call only *reports* the commands; `confirm=true` runs them through `ExecTool`. A user-configurable `skills.install_policy` (`never` | `approve` | `auto`) governs the behavior. §5.2 is a prompt-only note in the two dream prompts.

**Tech Stack:** Python 3, pytest, durin's `Tool` base + `ExecTool` (exec gate), existing `skills_import` / `skill_scan` modules.

**Roadmap:** `docs/plans/skills_evolutivas.md` (P6); backlog `docs/backlog.md` (P6 item #1).

## Design decisions (review these first)

1. **User-configurable policy `skills.install_policy` (`never` | `approve` | `auto`), default `approve`** ([[feedback_user_configurable_optional_features]]). `never` = the v1 behavior (info-only; the tool reports but never runs, even with `confirm=true`). `approve` = dry-run→confirm (default). `auto` = run without per-call confirm (openclaw-style, for users who opt in). The tool always still runs through `ExecTool`'s gate regardless of policy.
2. **Run through `ExecTool` (durin's single exec gate), not a bespoke subprocess.** Mirrors hermes (one command-approval gate for everything). Each confirmed command goes to `ExecTool.execute`, inheriting allow/deny patterns + sandbox + workspace boundary + logging. No second un-gated execution path.
3. **Only run specs the §8.C scanner did NOT flag dangerous.** Reuse `validate_install_specs`; any spec with a `dangerous` finding is dropped from the runnable set. (This static spec scan is a layer hermes lacks — hermes gates only at exec time.)
4. **`download` (URL) kind excluded in v1** — fetching+running an arbitrary URL is the riskiest; report "install manually." Package-manager kinds only: brew, apt, pip, cargo, npm, go, uv.
5. **No privilege escalation; surface sudo explicitly.** We never inject `sudo`. A command that plainly needs root (e.g. `apt-get install`) is **flagged `needs_privileges` in the dry-run** so the user sees it before approving; it still runs through `ExecTool` (whose policy decides) and a permission failure is reported, not silently retried with sudo.
6. **§5.2 is prompt-only** (no code, no UI): tell the dream LLM that the original lives in git.

## File Structure

- **Modify** `durin/agent/skills_import.py` — add `runnable_install_specs(skill_dir) -> list[dict]` (structured specs → command, dangerous dropped, download excluded, `needs_privileges` flagged).
- **Modify** `durin/config/schema.py` — add `skills.install_policy` (`never`|`approve`|`auto`).
- **Create** `durin/agent/tools/skill_install_deps.py` — `SkillInstallDepsTool` (policy-aware; runs commands through `ExecTool`).
- **Modify** `durin/templates/agent/dream_phase2.md` and `durin/templates/agent/skill_curation.md` — §5.2 git-original note.
- **Create** `tests/agent/test_runnable_install_specs.py`, `tests/config/test_skills_install_policy.py`, `tests/agent/test_skill_install_deps_tool.py`.

---

## Task 1: `runnable_install_specs` — safe specs → commands

**Files:**
- Modify: `durin/agent/skills_import.py`
- Test: `tests/agent/test_runnable_install_specs.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/agent/test_runnable_install_specs.py
"""P6 — runnable_install_specs: safe specs become commands; dangerous/download dropped."""
from pathlib import Path

from durin.agent.skills_import import runnable_install_specs


def _skill(tmp_path: Path, frontmatter: str) -> Path:
    d = tmp_path / "s"
    d.mkdir()
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\nbody\n", encoding="utf-8")
    return d


def test_brew_and_npm_become_commands(tmp_path):
    fm = (
        "name: s\n"
        "metadata:\n"
        "  tools:\n"
        "    install:\n"
        "      - {kind: brew, formula: gh}\n"
        "      - {kind: npm, package: prettier}\n"
    )
    out = runnable_install_specs(_skill(tmp_path, fm))
    cmds = [s["command"] for s in out]
    assert "brew install gh" in cmds
    assert "npm install -g prettier" in cmds
    assert all(s["needs_privileges"] is False for s in out)  # brew/npm = user-level


def test_apt_flagged_needs_privileges(tmp_path):
    fm = (
        "name: s\n"
        "metadata:\n"
        "  tools:\n"
        "    install:\n"
        "      - {kind: apt, package: ripgrep}\n"
    )
    out = runnable_install_specs(_skill(tmp_path, fm))
    assert out[0]["command"] == "apt-get install -y ripgrep"
    assert out[0]["needs_privileges"] is True


def test_download_kind_excluded(tmp_path):
    fm = (
        "name: s\n"
        "metadata:\n"
        "  tools:\n"
        "    install:\n"
        "      - {kind: download, url: 'https://example.com/x.sh'}\n"
    )
    out = runnable_install_specs(_skill(tmp_path, fm))
    assert out == []


def test_dangerous_spec_dropped(tmp_path):
    # an unsafe npm spec (shell metachars) is flagged dangerous by validate_install_specs
    fm = (
        "name: s\n"
        "metadata:\n"
        "  tools:\n"
        "    install:\n"
        "      - {kind: npm, package: 'evil && rm -rf /'}\n"
    )
    out = runnable_install_specs(_skill(tmp_path, fm))
    assert out == []


def test_no_specs_returns_empty(tmp_path):
    out = runnable_install_specs(_skill(tmp_path, "name: s\n"))
    assert out == []
```

- [ ] **Step 2: Run tests, verify they FAIL**

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_runnable_install_specs.py -q`
Expected: FAIL — `ImportError: cannot import name 'runnable_install_specs'`.

- [ ] **Step 3: Implement**

First read the existing `declared_install_specs` (`durin/agent/skills_import.py`, ~line 415) and `validate_install_specs` (`durin/security/skill_scan.py`, ~line 90) to match how specs are parsed (`metadata.<vendor>.install` is a list of `{kind, formula|package|module|cask|url}` dicts) and how `dangerous` findings are shaped (`Finding(category="install_spec", severity="dangerous", where=..., msg=...)`). Then add to `skills_import.py`:

```python
# kind → (command template, which spec field holds the value)
_INSTALL_CMDS = {
    "brew": ("brew install {v}", ("formula", "cask", "package")),
    "apt": ("apt-get install -y {v}", ("package",)),
    "pip": ("pip install {v}", ("package",)),
    "cargo": ("cargo install {v}", ("package",)),
    "npm": ("npm install -g {v}", ("package",)),
    "go": ("go install {v}", ("module", "package")),
    "uv": ("uv pip install {v}", ("package",)),
    # 'download' (url) intentionally excluded — install manually (v1).
}
# Kinds that plainly need root — surfaced as needs_privileges so the user sees it
# in the dry-run. We never inject sudo (decision 5).
_NEEDS_PRIV = {"apt"}


def runnable_install_specs(skill_dir) -> list[dict]:
    """Safe, runnable install specs as ``[{kind, value, command}]``. A spec the
    §8.C scanner flags ``dangerous`` is dropped; the ``download`` kind is excluded
    (install manually). No execution here — see the skill_install_deps tool."""
    from pathlib import Path

    from durin.security.skill_scan import validate_install_specs
    from durin.utils.frontmatter import split_frontmatter  # match the real import used by declared_install_specs

    md = Path(skill_dir) / "SKILL.md"
    if not md.is_file():
        return []
    try:
        data, _ = split_frontmatter(md.read_text(encoding="utf-8"))
    except OSError:
        return []

    # Spec coordinates the §8.C scanner already flagged dangerous → drop them.
    bad = {(f.where) for f in validate_install_specs(data) if f.severity == "dangerous"}

    out: list[dict] = []
    meta = data.get("metadata")
    if not isinstance(meta, dict):
        return out
    for vendor, blob in meta.items():
        specs = blob.get("install") if isinstance(blob, dict) else None
        if not isinstance(specs, list):
            continue
        for i, spec in enumerate(specs):
            if not isinstance(spec, dict):
                continue
            kind = str(spec.get("kind", ""))
            tmpl = _INSTALL_CMDS.get(kind)
            if tmpl is None:  # download / unknown → not runnable
                continue
            template, fields = tmpl
            value = next((str(spec[f]) for f in fields if spec.get(f)), "")
            if not value:
                continue
            # skip if this spec coordinate was flagged dangerous
            where = f"metadata.{vendor}.install[{i}]"
            if where in bad:
                continue
            out.append({"kind": kind, "value": value,
                        "command": template.format(v=value),
                        "needs_privileges": kind in _NEEDS_PRIV})
    return out
```

> IMPORTANT: the `where` string built here MUST match the format `validate_install_specs`
> emits in its `Finding.where` (read `skill_scan.py` and copy the exact pattern — it may
> be `metadata.<vendor>.install[<i>]` or similar). If they differ, the dangerous-drop
> won't work. Adjust to match, and confirm `test_dangerous_spec_dropped` passes for the
> real reason (spec dropped because flagged), not because npm parsing failed. Also confirm
> the real frontmatter splitter import path (`declared_install_specs` uses `split_frontmatter`
> — reuse the same import).

- [ ] **Step 4: Run tests, verify PASS**

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_runnable_install_specs.py -q`
Expected: PASS (5 passed). If `test_dangerous_spec_dropped` fails, fix the `where` format to match `validate_install_specs`.

- [ ] **Step 5: Commit**

```bash
git add durin/agent/skills_import.py tests/agent/test_runnable_install_specs.py
git commit -m "feat(skills): P6 runnable_install_specs — safe specs to commands (dangerous/download dropped)"
```

---

## Task 2a: add `skills.install_policy` to config

**Files:**
- Modify: `durin/config/schema.py` (the skills config section — find the class that holds `skills.security` / `skills.discovery`, ~line 420-470)
- Test: `tests/config/test_skills_install_policy.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_skills_install_policy.py
"""P6 — skills.install_policy default + acceptance."""
from durin.config.loader import load_config_from_dict  # match the real loader entrypoint


def test_install_policy_default_is_approve():
    c = load_config_from_dict({})
    assert c.skills.install_policy == "approve"


def test_install_policy_accepts_never_and_auto():
    for v in ("never", "approve", "auto"):
        c = load_config_from_dict({"skills": {"install_policy": v}})
        assert c.skills.install_policy == v
```

> Match the real config-construction entrypoint: grep `def load_config` / how other
> config tests build a `Config` from a dict (it may be `Config(**d)`, a
> `load_config_from_dict`, or a pydantic `model_validate`). Use whatever the existing
> `tests/config/` tests use; keep the assertion intent.

- [ ] **Step 2: Run, verify FAIL** (no `install_policy` field).

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/config/test_skills_install_policy.py -q`

- [ ] **Step 3: Add the field.** In `durin/config/schema.py`, on the skills config model (the one exposing `.security` and `.discovery`), add:

```python
    install_policy: Literal["never", "approve", "auto"] = "approve"
    """P6 #1 — how `skill_install_deps` runs a skill's declared install specs.
    'never' = report only (never run, even with confirm); 'approve' = dry-run then
    run on confirm (default); 'auto' = run without a per-call confirm. All policies
    still execute through ExecTool's gate."""
```

(`Literal` is already imported in `schema.py`; if not, add `from typing import Literal`.)

- [ ] **Step 4: Run, verify PASS** (2 passed).

- [ ] **Step 5: Commit**

```bash
git add durin/config/schema.py tests/config/test_skills_install_policy.py
git commit -m "feat(config): skills.install_policy (never|approve|auto) for P6 install executor"
```

---

## Task 2b: `skill_install_deps` tool (policy-aware, runs through ExecTool)

**Files:**
- Create: `durin/agent/tools/skill_install_deps.py`
- Test: `tests/agent/test_skill_install_deps_tool.py`

**Pattern note:** mirror the exact `Tool` shape of `durin/agent/tools/skill_write.py` (it uses `@tool_parameters(_PARAMETERS)` + `tool_parameters_schema`, a `name` property, `create(cls, ctx)`, async `execute`). Read it first and match it. The tool delegates execution to `ExecTool` — read `durin/agent/tools/shell.py` to confirm `ExecTool.create(ctx)` exists and `ExecTool.execute(command=...)` returns the command output as a string.

- [ ] **Step 1: Write the failing tests**

```python
# tests/agent/test_skill_install_deps_tool.py
"""P6 — skill_install_deps: policy-aware, runs each command through an exec runner."""
import asyncio

from durin.agent.tools.skill_install_deps import SkillInstallDepsTool

_SPEC = [{"kind": "brew", "value": "gh", "command": "brew install gh",
          "needs_privileges": False}]


def _tool(tmp_path, policy, ran):
    async def _exec(command, **_):
        ran.append(command)
        return f"ran: {command}"
    return SkillInstallDepsTool(workspace=tmp_path, exec_run=_exec, policy=policy)


def test_tool_name(tmp_path):
    assert _tool(tmp_path, "approve", []).name == "skill_install_deps"


def test_dry_run_lists_without_running(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.agent.skills_import.runnable_install_specs", lambda d: _SPEC)
    ran: list = []
    out = asyncio.run(_tool(tmp_path, "approve", ran).execute(name="demo", confirm=False))
    assert out["would_run"] == ["brew install gh"]
    assert ran == [] and out["ran"] is False


def test_confirm_runs_through_exec(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.agent.skills_import.runnable_install_specs", lambda d: _SPEC)
    ran: list = []
    out = asyncio.run(_tool(tmp_path, "approve", ran).execute(name="demo", confirm=True))
    assert ran == ["brew install gh"]          # went through the exec runner
    assert out["ran"] is True
    assert out["results"][0]["command"] == "brew install gh"


def test_policy_never_never_runs(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.agent.skills_import.runnable_install_specs", lambda d: _SPEC)
    ran: list = []
    out = asyncio.run(_tool(tmp_path, "never", ran).execute(name="demo", confirm=True))
    assert ran == [] and out["ran"] is False   # 'never' ignores confirm
    assert "never" in out["note"]


def test_policy_auto_runs_without_confirm(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.agent.skills_import.runnable_install_specs", lambda d: _SPEC)
    ran: list = []
    out = asyncio.run(_tool(tmp_path, "auto", ran).execute(name="demo", confirm=False))
    assert ran == ["brew install gh"] and out["ran"] is True


def test_dry_run_flags_privileges(monkeypatch, tmp_path):
    spec = [{"kind": "apt", "value": "ripgrep", "command": "apt-get install -y ripgrep",
             "needs_privileges": True}]
    monkeypatch.setattr("durin.agent.skills_import.runnable_install_specs", lambda d: spec)
    out = asyncio.run(_tool(tmp_path, "approve", []).execute(name="demo", confirm=False))
    assert out["needs_privileges"] == ["apt-get install -y ripgrep"]
```

- [ ] **Step 2: Run, verify FAIL** — module not found.

- [ ] **Step 3: Implement** `durin/agent/tools/skill_install_deps.py` (adjust Tool-base shape to match `skill_write.py`):

```python
"""skill_install_deps tool — P6 #1. Install a skill's DECLARED dependencies, only
after explicit user approval. The default call is a DRY RUN that lists the exact
commands; ``confirm=true`` runs them. Each command is executed through durin's single
exec gate (ExecTool) — same allow/deny patterns, sandbox, and logging as any shell
command — not a side-channel subprocess. Policy `skills.install_policy`: 'never' =
report only; 'approve' = dry-run→confirm (default); 'auto' = run without confirm.
Only specs the §8.C scanner rated safe are runnable; 'download' kind and sudo are
excluded — privileged commands are flagged `needs_privileges` for the user."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from durin.agent.tools.base import Tool
from durin.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema

_PARAMETERS = tool_parameters_schema(
    name=StringSchema("Name of the skill whose declared dependencies to install."),
    confirm=BooleanSchema(
        "false (default) = DRY RUN: only report the commands. true = run them — pass "
        "true ONLY after the user explicitly approved the listed commands."),
    required=["name"],
    description=(
        "Install a skill's declared dependencies (its `install` specs) after user "
        "approval. Default is a dry run listing the exact commands; call again with "
        "confirm=true to run them once the user approved. Commands run through "
        "durin's exec gate; only safe package-manager specs are runnable; never "
        "escalates privileges (privileged ones are flagged for you)."
    ),
)


def _skill_dir(workspace: Path, name: str) -> Path:
    return Path(workspace) / "skills" / name


class SkillInstallDepsTool(Tool):
    """Install a skill's declared deps via ExecTool; dry-run by default."""

    def __init__(self, workspace, exec_run: Callable[..., Awaitable[str]],
                 policy: str = "approve") -> None:
        self._workspace = Path(workspace)
        self._exec_run = exec_run        # async (command=...) -> output str
        self._policy = policy

    @property
    def name(self) -> str:
        return "skill_install_deps"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @property
    def parameters(self) -> dict:
        return _PARAMETERS

    @classmethod
    def create(cls, ctx: Any) -> "SkillInstallDepsTool":
        from durin.agent.tools.shell import ExecTool
        exec_tool = ExecTool.create(ctx)
        policy = "approve"
        try:
            policy = ctx.app_config.skills.install_policy
        except Exception:  # noqa: BLE001
            try:
                from durin.config.loader import load_config
                policy = load_config().skills.install_policy
            except Exception:  # noqa: BLE001
                pass
        return cls(workspace=ctx.workspace, exec_run=exec_tool.execute, policy=policy)

    async def execute(self, **kwargs: Any) -> Any:
        from durin.agent.skills_import import runnable_install_specs

        name = str(kwargs.get("name", "")).strip()
        confirm = bool(kwargs.get("confirm", False))
        specs = runnable_install_specs(_skill_dir(self._workspace, name))
        commands = [s["command"] for s in specs]
        privileged = [s["command"] for s in specs if s.get("needs_privileges")]
        if not commands:
            return {"would_run": [], "ran": False,
                    "note": "no safe, runnable install specs declared"}

        base = {"would_run": commands, "needs_privileges": privileged}
        if self._policy == "never":
            return {**base, "ran": False,
                    "note": "install_policy=never — reporting only, not running"}
        run = self._policy == "auto" or confirm
        if not run:
            return {**base, "ran": False,
                    "note": "DRY RUN — review with the user (note any needs_privileges), "
                            "then call again with confirm=true"}
        results = []
        for cmd in commands:
            output = await self._exec_run(command=cmd)
            results.append({"command": cmd, "output": str(output)[-2000:]})
        return {**base, "ran": True, "results": results}
```

> If `skill_write.py` attaches params via `@tool_parameters(_PARAMETERS)`, mirror that.
> Confirm `ExecTool.execute` is `async def execute(self, command, ...)` returning a
> string (read `shell.py`); if its first param differs, adapt the `_exec_run(command=cmd)`
> call to match.

- [ ] **Step 4: Run tests, verify PASS** (6 passed).

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_skill_install_deps_tool.py -q`

- [ ] **Step 5: Commit**

```bash
git add durin/agent/tools/skill_install_deps.py tests/agent/test_skill_install_deps_tool.py
git commit -m "feat(skills): P6 #1 skill_install_deps tool — policy-aware, runs through ExecTool"
```

---

## Task 3: §5.2 — make the git-original explicit to the dream LLM

**Files:**
- Modify: `durin/templates/agent/dream_phase2.md`
- Modify: `durin/templates/agent/skill_curation.md`

- [ ] **Step 1: Read both prompts**

Run: `sed -n '1,40p' durin/templates/agent/dream_phase2.md` and `sed -n '1,40p' durin/templates/agent/skill_curation.md`. Find a natural spot near where each instructs editing/creating skills.

- [ ] **Step 2: Add the note to `dream_phase2.md`** (after the "Skill creation rules" heading, near the existing rules):

```markdown
- **The original is safe in git.** Every skill's as-imported / as-created content is its FIRST commit in the skills git store; each of your edits is a further commit. The original is never lost — it is always recoverable and diffable (`git diff` import→HEAD). Edit freely toward improvement; you are never overwriting the original, only versioning on top of it.
```

- [ ] **Step 3: Add the same note to `skill_curation.md`** (near where it describes `evolve`/`fuse`):

```markdown
- **The original is safe in git.** Each skill's original content is its first commit; your `evolve`/`fuse` edits are versioned on top. The original is always recoverable/diffable — evolve toward a concrete improvement without fear of losing the original.
```

- [ ] **Step 4: Verify both templates render**

Run:
```bash
/Users/marcelo/git_personal/durin/.venv/bin/python -c "from durin.agent.context import render_template as r; print('phase2 ok' if 'original is safe in git' in r('agent/dream_phase2.md', skill_creator_path='/x').lower() else 'MISS'); print('curation ok' if 'original is safe in git' in r('agent/skill_curation.md').lower() else 'MISS')"
```
Expected: `phase2 ok` and `curation ok` (adjust the render call args to whatever each template needs — grep `render_template('agent/skill_curation` in the codebase for the real call).

- [ ] **Step 5: Commit**

```bash
git add durin/templates/agent/dream_phase2.md durin/templates/agent/skill_curation.md
git commit -m "docs(skills): §5.2 — make the git-preserved original explicit to the dream LLM"
```

---

## Task 4: Live verification

- [ ] **Step 1: All new tests green**

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_runnable_install_specs.py tests/config/test_skills_install_policy.py tests/agent/test_skill_install_deps_tool.py -q`
Expected: PASS (13 passed: 5 + 2 + 6).

- [ ] **Step 2: Regression**

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/ tests/config/ -k "skill or dream or tool or config" -q`
Expected: PASS (no regressions).

- [ ] **Step 3: Live dry-run against a real skill with install specs**

Author a workspace skill whose frontmatter declares a safe `install` spec (e.g.
`metadata.tools.install: [{kind: brew, formula: jq}]`), then verify the parse→command
path directly (no tool/ctx needed for the dry-run logic):
```bash
/Users/marcelo/git_personal/durin/.venv/bin/python -c "
from durin.agent.skills_import import runnable_install_specs
print(runnable_install_specs('<workspace>/skills/<skill>'))"
```
Expected: `[{'kind': 'brew', 'value': 'jq', 'command': 'brew install jq', 'needs_privileges': False}]` — confirms parse→command end to end.

- [ ] **Step 4: Live tool dry-run through `create(ctx)`** — run the real agent
(`durin agent -m "..."`) with a task that needs an unavailable skill, and confirm it
calls `skill_install_deps` and reports a dry-run (the agent should ask before
`confirm=true`). Capture the transcript. (Skip the actual install unless you want to
mutate this machine; if so, a harmless dep like `jq` via `confirm=true`.)

- [ ] **Step 5: Mark P6 #1 built**

Update `docs/backlog.md` P6 to note item #1 is built (executor: dry-run→confirm,
policy-configurable, runs through ExecTool). Items #2 (skill-script execution via the
tool gate) and #3 (per-skill sandbox) still pending. Commit.

---

## Self-Review

**Coverage:** P6 #1 (offer to run install specs with explicit approval) → Tasks 1, 2a (policy config), 2b (policy-aware tool running through ExecTool). §5.2 (git-original explicit to dream LLM) → Task 3. Live verify → Task 4.

**Design honored:** policy `never|approve|auto` (default `approve`); commands run through `ExecTool` (one gate, hermes-style), not a bespoke subprocess; dangerous specs dropped (reuse `validate_install_specs`); `download` excluded; sudo never injected, `needs_privileges` flagged in the dry-run; §5.2 prompt-only.

**Placeholder scan:** every code/prompt step shows complete content. The "match the real `where`/render/Tool-base shape / loader entrypoint / ExecTool.execute signature" notes are guardrails against API drift, not placeholders.

**Type consistency:** `runnable_install_specs(skill_dir) -> [{kind, value, command, needs_privileges}]` is produced in Task 1 and consumed by the tool in Task 2b (reads `s["command"]`, `s["needs_privileges"]`). The tool's `exec_run` is an async `(command=...) -> str` injected in tests and wired to `ExecTool.execute` in `create(ctx)` — same shape both places.

## Out of scope (explicit)

- P6 **#2** (run a skill's **bundled scripts** — `scripts/` — through durin's tool gate) and **#3** (per-skill FS/net sandbox) — separate, larger. (This plan already routes *install commands* through the exec gate; #2 is about skill-authored scripts.)
- `download`-kind install specs and privileged/sudo installs — surfaced but install by hand.
- Any UI for §5.2 (the user deferred the visual part).
