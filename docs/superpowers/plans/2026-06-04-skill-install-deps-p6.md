# P6 #1 — Approved install-spec executor (+ §5.2 git-original prompt) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let durin install a skill's declared dependencies on demand, but only after explicit user approval — closing the gap where a skill is "unavailable" (missing CLI) and the user has to install deps by hand. Plus make it explicit to the dream LLM that a skill's original lives in git (§5.2).

**Architecture:** Today `declared_install_specs` parses + `validate_install_specs` scans install specs, and they are surfaced per skill, but durin never runs them (policy `never`). This adds (a) a helper that turns *safe* install specs into concrete shell commands, and (b) an agent tool `skill_install_deps(name, confirm)` that follows durin's existing dry-run→confirm convention (like `skill_edit`/import): the first call (default) only *reports* what it would run; `confirm=true` runs it. The agent is instructed to get the user's OK before passing `confirm=true` — `ask_user_question` is a turn-pause, so approval is a conversational step, not an in-call return. §5.2 is a prompt-only note in the two dream prompts.

**Tech Stack:** Python 3, pytest, `subprocess`, durin's `Tool` base, existing `skills_import` / `skill_scan` modules.

**Roadmap:** `docs/plans/skills_evolutivas.md` (P6); backlog `docs/backlog.md` (P6 item #1).

## Design decisions (review these first)

1. **Dry-run→confirm, not auto, not never.** `skill_install_deps(name)` defaults to a dry-run that lists the exact commands; `confirm=true` executes. Mirrors `skill_edit`'s `confirm` gate. This is the "middle ground between hermes=never and openclaw=auto" the backlog asks for. The user's approval happens in chat before the agent passes `confirm=true`.
2. **Only run specs the §8.C scanner did NOT flag dangerous.** Reuse `validate_install_specs`; any spec with a `dangerous` finding is dropped from the runnable set (reported as skipped).
3. **`download` (URL) kind is excluded in v1** — fetching+running an arbitrary URL is the riskiest; report it as "install manually." Package-manager kinds only: brew, apt, pip, cargo, npm, go, uv.
4. **No privilege escalation.** Commands run as the agent's user via `subprocess`; we never inject `sudo`. A command that needs privileges fails and is reported — the user runs it manually. Routing through durin's exec/tool gate is P6 #2 (out of scope here).
5. **§5.2 is prompt-only** (no code, no UI): tell the dream LLM that the original lives in git.

## File Structure

- **Modify** `durin/agent/skills_import.py` — add `runnable_install_specs(skill_dir) -> list[dict]` (structured specs → command, dangerous dropped, download excluded).
- **Create** `durin/agent/tools/skill_install_deps.py` — `SkillInstallDepsTool` (dry-run/confirm executor).
- **Modify** `durin/templates/agent/dream_phase2.md` and `durin/templates/agent/skill_curation.md` — §5.2 git-original note.
- **Create** `tests/agent/test_runnable_install_specs.py`, `tests/agent/test_skill_install_deps_tool.py`.

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
                        "command": template.format(v=value)})
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
Expected: PASS (4 passed). If `test_dangerous_spec_dropped` fails, fix the `where` format to match `validate_install_specs`.

- [ ] **Step 5: Commit**

```bash
git add durin/agent/skills_import.py tests/agent/test_runnable_install_specs.py
git commit -m "feat(skills): P6 runnable_install_specs — safe specs to commands (dangerous/download dropped)"
```

---

## Task 2: `skill_install_deps` tool (dry-run → confirm)

**Files:**
- Create: `durin/agent/tools/skill_install_deps.py`
- Test: `tests/agent/test_skill_install_deps_tool.py`

**Pattern note:** mirror the exact `Tool` shape of `durin/agent/tools/skill_write.py` (it uses `@tool_parameters(_PARAMETERS)` + `tool_parameters_schema`, a `name` property, `create(cls, ctx)` reading `ctx.workspace`, async `execute`). Read it first and match it.

- [ ] **Step 1: Write the failing tests**

```python
# tests/agent/test_skill_install_deps_tool.py
"""P6 — skill_install_deps: dry-run lists commands; confirm runs them."""
import asyncio

from durin.agent.tools.skill_install_deps import SkillInstallDepsTool


def test_tool_name(tmp_path):
    assert SkillInstallDepsTool(workspace=tmp_path).name == "skill_install_deps"


def test_dry_run_lists_commands_without_running(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "durin.agent.skills_import.runnable_install_specs",
        lambda d: [{"kind": "brew", "value": "gh", "command": "brew install gh"}])
    ran = []
    monkeypatch.setattr(
        "durin.agent.tools.skill_install_deps._run_command",
        lambda cmd: ran.append(cmd) or {"command": cmd, "ok": True, "output": ""})
    tool = SkillInstallDepsTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(name="demo", confirm=False))
    assert out["would_run"] == ["brew install gh"]
    assert ran == []  # dry-run never executes
    assert out["ran"] is False


def test_confirm_runs_commands(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "durin.agent.skills_import.runnable_install_specs",
        lambda d: [{"kind": "brew", "value": "gh", "command": "brew install gh"}])
    ran = []
    monkeypatch.setattr(
        "durin.agent.tools.skill_install_deps._run_command",
        lambda cmd: ran.append(cmd) or {"command": cmd, "ok": True, "output": "done"})
    tool = SkillInstallDepsTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(name="demo", confirm=True))
    assert ran == ["brew install gh"]
    assert out["ran"] is True
    assert out["results"][0]["ok"] is True


def test_no_specs_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "durin.agent.skills_import.runnable_install_specs", lambda d: [])
    tool = SkillInstallDepsTool(workspace=tmp_path)
    out = asyncio.run(tool.execute(name="demo", confirm=True))
    assert out["would_run"] == []
    assert out.get("ran") is False
```

- [ ] **Step 2: Run tests, verify they FAIL**

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_skill_install_deps_tool.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement** `durin/agent/tools/skill_install_deps.py` (adjust the Tool-base shape to match `skill_write.py`):

```python
"""skill_install_deps tool — P6 #1. Install a skill's DECLARED dependencies, but
only after explicit user approval. Default call is a DRY RUN that lists the exact
commands it would run; ``confirm=true`` runs them. Get the user's OK before passing
confirm=true. Only specs the §8.C scanner rated safe are runnable; the 'download'
kind and privileged installs are out of scope (install those by hand)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool
from durin.agent.tools.schema import BooleanSchema, StringSchema, tool_parameters_schema

_PARAMETERS = tool_parameters_schema(
    name=StringSchema("Name of the skill whose declared dependencies to install."),
    confirm=BooleanSchema(
        "false (default) = DRY RUN: only report the commands. true = actually run "
        "them — pass true ONLY after the user explicitly approved the commands."),
    required=["name"],
    description=(
        "Install a skill's declared dependencies (its `install` specs) after user "
        "approval. Default is a dry run listing the exact commands; call again with "
        "confirm=true to run them once the user has approved. Only safe, "
        "package-manager specs are runnable; never escalates privileges."
    ),
)


def _run_command(cmd: str) -> dict:
    """Run one install command, capturing output. No shell metacharacters are
    introduced by us — the command comes from a validated package name."""
    try:
        proc = subprocess.run(
            cmd.split(), capture_output=True, text=True, timeout=600)
        return {"command": cmd, "ok": proc.returncode == 0,
                "output": (proc.stdout + proc.stderr)[-2000:]}
    except Exception as e:  # noqa: BLE001
        return {"command": cmd, "ok": False, "output": f"{type(e).__name__}: {e}"}


def _skill_dir(workspace: Path, name: str) -> Path:
    return Path(workspace) / "skills" / name


class SkillInstallDepsTool(Tool):
    """Install a skill's declared deps, dry-run by default, confirm to run."""

    def __init__(self, workspace) -> None:
        self._workspace = Path(workspace)

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
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        from durin.agent.skills_import import runnable_install_specs

        name = str(kwargs.get("name", "")).strip()
        confirm = bool(kwargs.get("confirm", False))
        specs = runnable_install_specs(_skill_dir(self._workspace, name))
        commands = [s["command"] for s in specs]
        if not commands:
            return {"would_run": [], "ran": False,
                    "note": "no safe, runnable install specs declared"}
        if not confirm:
            return {"would_run": commands, "ran": False,
                    "note": "DRY RUN — review with the user, then call again with "
                            "confirm=true to run these"}
        results = [_run_command(c) for c in commands]
        return {"would_run": commands, "ran": True, "results": results}
```

> If `skill_write.py` attaches parameters via `@tool_parameters(_PARAMETERS)` (decorator)
> rather than a `parameters` property, mirror that exactly. Keep the test intent.

- [ ] **Step 4: Run tests, verify PASS**

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_skill_install_deps_tool.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add durin/agent/tools/skill_install_deps.py tests/agent/test_skill_install_deps_tool.py
git commit -m "feat(skills): P6 #1 skill_install_deps tool (dry-run by default, confirm to run)"
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

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/test_runnable_install_specs.py tests/agent/test_skill_install_deps_tool.py -q`
Expected: PASS (8 passed).

- [ ] **Step 2: Regression**

Run: `/Users/marcelo/git_personal/durin/.venv/bin/python -m pytest tests/agent/ -k "skill or dream or tool" -q`
Expected: PASS (no regressions).

- [ ] **Step 3: Live dry-run against a real skill with install specs**

Find or author a workspace skill whose frontmatter declares a safe `install` spec (e.g. `metadata.tools.install: [{kind: brew, formula: jq}]`), then:
```bash
/Users/marcelo/git_personal/durin/.venv/bin/python -c "
import asyncio; from durin.agent.tools.skill_install_deps import SkillInstallDepsTool
t = SkillInstallDepsTool(workspace='<workspace-with-that-skill>')
print(asyncio.run(t.execute(name='<skill>', confirm=False)))"
```
Expected: `{'would_run': ['brew install jq'], 'ran': False, ...}` — confirms parse→command→dry-run end to end without executing.

- [ ] **Step 4: (Optional) Live confirm run** — only if you want to actually install a harmless dep (e.g. `jq`) on this machine: re-run with `confirm=True` and confirm it installs and reports `ok`. Skip if you don't want to mutate the system.

- [ ] **Step 5: Mark P6 #1 built**

Update `docs/backlog.md` P6 to note item #1 is built (executor with approval; items #2 exec-gate and #3 sandbox still pending). Commit.

---

## Self-Review

**Coverage:** P6 #1 (offer to run install specs with explicit approval) → Tasks 1–2 (safe-spec→command + dry-run/confirm tool). §5.2 (git-original explicit to dream LLM) → Task 3. Live verify → Task 4.

**Design honored:** dry-run→confirm (not auto, not never) = the backlog's middle ground; dangerous specs dropped (reuse `validate_install_specs`); download/privileged excluded; §5.2 prompt-only.

**Placeholder scan:** every code/prompt step shows complete content. The two "match the real `where`/render/Tool-base shape" notes are guardrails against API drift, not placeholders.

**Type consistency:** `runnable_install_specs(skill_dir) -> [{kind, value, command}]` is produced in Task 1 and consumed by the tool in Task 2 (reads `s["command"]`). `_run_command(cmd) -> {command, ok, output}` is defined and patched identically in the tool + tests.

## Out of scope (explicit)

- P6 #2 (route skill-script execution through durin's tool/exec gate) and #3 (per-skill FS/net sandbox) — separate, larger.
- `download`-kind install specs and privileged (sudo) installs — install by hand.
- Any UI for §5.2 (the user deferred the visual part).
