"""Agentic, transactional skill restructure — the composition-repair executor.

The curation judge only DECIDES that a skill must be restructured (and why); this
module EXECUTES it. An agentic sub-agent authors the fix into an ISOLATED staging
copy of the skill using real tools — read the skill's files, edit SKILL.md, bundle
a script via write_file, author a workflow to delegate to — never by pasting whole
files into a single completion. The staged result is then VALIDATED (integrity
floor + composition gate + security scan) and only a validated, complete result is
applied to the live workspace via the existing locked commit; otherwise it is
discarded and the live skill is untouched.

This replaces the earlier `restructure` shape where a tool-less judge emitted the
whole new SKILL.md body + script contents + workflow graph inline in one JSON blob
— a shape that corrupted a skill when the completion truncated (a half-body, zero
scripts, written straight to the live file). Here a truncated author simply fails
validation in staging and the live skill never changes.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_RESTRUCTURE_PROMPT = """You are durin's skill-restructure pass. A curation judge \
decided this skill must be restructured to satisfy the composition doctrine. \
Do it by EDITING FILES WITH YOUR TOOLS — never by pasting whole files into a reply.

{doctrine}

Workflows you can delegate to (use list_workflows for detail):
{workflow_catalog}

If you author a NEW workflow with workflow_write, this is the definition format \
(note the deterministic `script` node kind — use it for command/check/transform \
steps instead of an agent node):

{workflow_authoring}

The skill lives under `skills/{name}/`. Its current SKILL.md:
---
{current}
---

INTENT — what to fix and why:
{intent}

How to work (tools operate under `skills/{name}/`):
- Lift inline deterministic code into a bundled script: `write_file` \
`skills/{name}/scripts/<x>.py` taking its input as an argument, then `edit_file` \
the SKILL.md body to invoke it by path and DELETE the inline code block.
- Replace a narrated workflow-shaped procedure: if a workflow above already \
covers it, rewrite the body to run it via run_workflow; if none does, author one \
with workflow_write, then rewrite the body to delegate to it. Inside a workflow, \
give deterministic steps (run a command, transform text, gate on a check) a \
`script` node — a subprocess, not an agent turn — per the `workflows` skill.
- Keep the skill's domain knowledge and its frontmatter (name, description). \
NEVER leave the body a truncated stub — it must remain a complete, usable skill.

Make the edits, then stop.
"""


def restructure_skill_agentic(
    workspace: Path, name: str, *, intent: str,
    provider: Any | None = None, model: str | None = None,
) -> dict:
    """Sync entry point (curation calls this from its worker thread). Runs the
    agentic restructure of ``name`` toward ``intent`` and returns
    ``{"applied": bool, ...}``. Never raises for an authoring failure — a failed
    restructure returns ``applied=False`` with a reason and leaves the live skill
    untouched."""
    import asyncio
    try:
        return asyncio.run(_restructure_async(
            workspace, name, intent=intent, provider=provider, model=model))
    except Exception as exc:  # noqa: BLE001 — a restructure failure must not break the pass
        logger.exception("agentic restructure of %s failed", name)
        return {"applied": False, "error": str(exc)}


async def _restructure_async(
    workspace: Path, name: str, *, intent: str, provider: Any | None, model: str | None,
) -> dict:
    from durin.agent import skills_store as ss
    from durin.agent.skills_doctrine import (
        composition_doctrine,
        judge_composition_async,
        workflow_authoring_reference,
        workflow_catalog_text,
    )

    workspace = Path(workspace)
    current = ss.read_skill_content(workspace, name)
    if current is None:
        return {"applied": False, "error": f"skill not found: {name}"}
    live_skill_dir = ss._skill_md(workspace, name).parent

    staging = Path(tempfile.mkdtemp(prefix=f"durin-restructure-{name}-"))
    try:
        # 1. Isolated staging: a copy of the skill + the live workflow defs (so
        # list_workflows shows real options and any authored workflow lands here).
        stg_skill = staging / "skills" / name
        stg_skill.parent.mkdir(parents=True)
        shutil.copytree(live_skill_dir, stg_skill)
        live_wf = workspace / "workflows"
        if live_wf.is_dir():
            shutil.copytree(live_wf, staging / "workflows",
                            ignore=shutil.ignore_patterns(".git"))

        # 2. Resolve provider/model (memory preset) — model and provider travel together.
        if provider is None or not model:
            from durin.config.loader import load_config
            from durin.memory.model_resolve import resolve_aux_preset
            from durin.providers.factory import make_provider
            config = load_config()
            preset = resolve_aux_preset(config, purpose="memory")
            model = model or preset.model
            provider = provider or make_provider(config, preset=preset)

        # 3. Tools scoped to STAGING — the sub-agent cannot touch the live tree.
        tools = _build_restructure_tools(staging)

        # 4. Run the agentic author.
        from durin.agent.runner import AgentRunner, AgentRunSpec
        prompt = _RESTRUCTURE_PROMPT.format(
            doctrine=composition_doctrine() or "(doctrine unavailable)",
            workflow_catalog=workflow_catalog_text(staging),
            workflow_authoring=workflow_authoring_reference() or "(authoring reference unavailable — rely on workflow_write's validation errors)",
            name=name, current=current, intent=intent,
        )
        await AgentRunner(provider).run(AgentRunSpec(
            initial_messages=[{"role": "user", "content": prompt}],
            tools=tools, model=model, max_iterations=8,
            max_tool_result_chars=8000, fail_on_tool_error=False,
            workspace=staging,
        ))

        # 5. Validate the staged result — nothing reaches live until this passes.
        staged_md_path = stg_skill / "SKILL.md"
        if not staged_md_path.exists():
            return {"applied": False, "error": "restructure left no SKILL.md"}
        staged_md = staged_md_path.read_text(encoding="utf-8")
        bad = ss._skill_md_integrity(staged_md)
        if bad is not None:
            return {"applied": False, "error": f"invalid result: {bad}"}
        ok, reason = await judge_composition_async(
            staged_md, staging, provider=provider, model=model)
        if not ok:
            return {"applied": False, "error": f"still violates doctrine: {reason}"}

        # 6. Gather staged bundled files (scripts) + security-scan the staged dir.
        files: dict[str, str] = {}
        for f in sorted(stg_skill.rglob("*")):
            if not f.is_file() or f.name == "SKILL.md":
                continue
            rel = f.relative_to(stg_skill).as_posix()
            if ss._safe_bundle_path(rel):
                files[rel] = f.read_text(encoding="utf-8")
        if files:
            from durin.security.skill_scan import scan_skill
            rep = scan_skill(stg_skill)
            if rep.verdict != "safe":
                return {"applied": False, "error": f"bundled code scanned {rep.verdict}"}

        # 7. Apply any NEW workflow the sub-agent authored (staging-only) to live,
        # FIRST — a skill that delegates to a workflow that failed to land would
        # dangle, so abort the whole restructure if a workflow apply fails.
        applied_workflows = _apply_new_workflows(staging, workspace)
        if applied_workflows is None:
            return {"applied": False, "error": "authored workflow failed to apply"}

        # 8. Atomic apply of the validated skill via the existing locked commit
        # path (composition_judge=None: already gated async above).
        r = ss.dream_restructure_skill(
            workspace, name, content=staged_md, files=files or None,
            rationale=f"restructure (agentic): {intent[:80]}",
            attribution=ss.Attribution(actor="curation"), composition_judge=None)
        return {"applied": bool(r.get("ok")), "workflows": applied_workflows, **r}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _build_restructure_tools(staging: Path) -> Any:
    """Read/write tools + workflow authoring, ALL scoped to the staging workspace
    so the sub-agent's edits never touch the live tree."""
    from durin.agent.tools.file_state import FileStates
    from durin.agent.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
    from durin.agent.tools.list_workflows import ListWorkflowsTool
    from durin.agent.tools.registry import ToolRegistry
    from durin.agent.tools.workflow_write import WorkflowWriteTool

    fs = FileStates()
    tools = ToolRegistry()
    tools.register(ReadFileTool(workspace=staging, allowed_dir=staging, file_states=fs))
    # guard_registry_dirs=False: `staging` is a throwaway tempdir copy (mirroring
    # the live layout as `staging/skills/<name>/` purely so the sub-agent's
    # path references line up), not the live skills registry — the generic
    # skills-write guard would otherwise block the very `skills/{name}/...`
    # writes this sub-agent is instructed to make. The staged result only
    # ever reaches the live tree through the validated, gated commit path in
    # `_restructure_async` (integrity check + composition judge + security
    # scan), so this isolated copy needs no additional path guarding.
    tools.register(WriteFileTool(workspace=staging, allowed_dir=staging, file_states=fs,
                                  guard_registry_dirs=False))
    tools.register(EditFileTool(workspace=staging, allowed_dir=staging, file_states=fs,
                                 guard_registry_dirs=False))
    tools.register(ListWorkflowsTool(workspace=staging))
    tools.register(WorkflowWriteTool(workspace=staging))
    return tools


def _apply_new_workflows(staging: Path, workspace: Path) -> list[str] | None:
    """Persist workflows the sub-agent authored in staging (those absent from live)
    to the live workspace via the sanctioned write path. Returns the list of
    applied names, or None if any failed to apply (caller aborts)."""
    import json

    from durin.workflow.editing import save_workflow_definition

    stg_wf = staging / "workflows"
    live_wf = workspace / "workflows"
    if not stg_wf.is_dir():
        return []
    applied: list[str] = []
    for f in sorted(stg_wf.glob("*.json")):
        if (live_wf / f.name).exists():
            continue  # pre-existing workflow, not authored this run
        try:
            definition = json.loads(f.read_text(encoding="utf-8"))
            res = save_workflow_definition(
                workspace, f.stem, definition,
                reason="authored by skill restructure", actor="curation",
                must_exist=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("restructure: authored workflow %s failed to load: %s", f.stem, exc)
            return None
        if not res.get("ok"):
            logger.warning("restructure: authored workflow %s rejected: %s", f.stem, res.get("error"))
            return None
        applied.append(f.stem)
    return applied
