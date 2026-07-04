"""workflow_write tool — the sanctioned workflow-authoring tool.

Auto-discovered into the main agent's ``core`` toolset (like skill_write), and
given to the dream's skill-extract subagent, so both authoring paths can create
a workflow instead of narrating an orchestration in skill prose. Persists
through the shared editing engine (validate → atomic write under the editor's
lock → version-store commit). Create-only: editing an existing workflow is
``workflow_edit``'s job.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import ObjectSchema, StringSchema, tool_parameters_schema

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
        from durin.workflow.editing import save_workflow_definition

        result = save_workflow_definition(
            self._workspace,
            str(kwargs.get("name", "")),
            kwargs.get("definition"),
            reason=str(kwargs.get("rationale", "")),
            actor="agent",
            must_exist=False,
        )
        if result.get("ok"):
            result["note"] = "run it with run_workflow(name, task)"
        return json.dumps(result)
