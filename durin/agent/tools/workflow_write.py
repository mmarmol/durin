"""workflow_write tool — the sanctioned workflow-authoring tool.

Auto-discovered into the main agent's ``core`` toolset (like skill_write), and
given to the dream's skill-extract subagent, so both authoring paths can create
a workflow instead of narrating an orchestration in skill prose. The definition
is validated as a graph (``parse_workflow``) before anything lands on disk,
written under the same cross-process lock the HTTP editor uses, and committed
to the workflow version store. Create-only: editing an existing workflow is the
editor's / improvement pass's job, not this tool's.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import ObjectSchema, StringSchema, tool_parameters_schema

logger = logging.getLogger(__name__)

_PARAMETERS = tool_parameters_schema(
    name=StringSchema("Name of the new workflow (file name, kebab-case)."),
    definition=ObjectSchema(
        description=(
            "The full workflow definition (the flow graph: description, start, "
            "nodes, input/output). See the `workflows` skill for the format."
        ),
        additional_properties=True,
    ),
    rationale=StringSchema(
        "Why this workflow is worth creating — recorded in the version history."
    ),
    required=["name", "definition", "rationale"],
    description=(
        "Create a new workflow definition (a flow graph run by run_workflow). The "
        "definition is validated as a graph before it is saved; schema errors come "
        "back verbatim. Create-only — it refuses to overwrite an existing workflow. "
        "Use when a recurring multi-step process earns engine execution (fan-out, "
        "verification gates, determinism) per the `workflows` skill; check "
        "`list_workflows` first so you extend the catalog, not duplicate it."
    ),
)


def _safe_name(name: str) -> bool:
    """Reject names that could escape the workflows dir (path traversal)."""
    return bool(name) and name not in (".", "..") and not any(
        c in name for c in ("/", "\\", "\x00")
    )


@tool_parameters(_PARAMETERS)
class WorkflowWriteTool(Tool):
    """workflow_write tool — validate and persist a NEW workflow definition."""

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "workflow_write"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> "WorkflowWriteTool":
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> str:
        from durin.utils.atomic_write import atomic_write_text
        from durin.utils.file_lock import cross_process_lock
        from durin.workflow.loader import workflows_dir
        from durin.workflow.spec import WorkflowError, parse_workflow
        from durin.workflow.version_store import WorkflowVersionStore, version_lock_target

        name = str(kwargs.get("name", "")).strip()
        definition = kwargs.get("definition")
        rationale = str(kwargs.get("rationale", "")).strip()
        if not _safe_name(name):
            return json.dumps({"error": "invalid workflow name"})
        if not isinstance(definition, dict):
            return json.dumps({"error": "definition must be a JSON object"})
        if not rationale:
            return json.dumps({"error": "rationale is required"})

        definition = dict(definition)
        definition["name"] = name                       # file name and inner name stay consistent
        definition.setdefault("improvement_mode", "manual")
        try:
            parse_workflow(definition)
        except WorkflowError as exc:
            return json.dumps({"error": f"invalid workflow: {exc}"})

        d = workflows_dir(self._workspace)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{name}.json"
        with cross_process_lock(version_lock_target(d)):
            if path.exists():
                return json.dumps({"error": f"workflow already exists: {name}"})
            atomic_write_text(path, json.dumps(definition, indent=2, ensure_ascii=False))
        sha = None
        try:
            sha = WorkflowVersionStore(d).commit_edit(name, rationale, actor="agent")
        except Exception as exc:  # noqa: BLE001 - versioning is best-effort, the write already landed
            logger.warning("workflow_write: version commit failed for %s: %s", name, exc)
        return json.dumps({"ok": True, "name": name, "commit": sha,
                           "note": "run it with run_workflow(name, task)"})
