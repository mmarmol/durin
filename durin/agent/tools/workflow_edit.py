"""workflow_edit tool — the sanctioned workflow-editing tool.

The edit-only complement of ``workflow_write``: it refuses a name that does not
exist yet, and persists through the same shared engine (graph validation →
atomic write under the editor's lock → version-store commit, actor="agent").
Available to the in-session agent only — the dream's edit path is the improve
pass, whose prompt-only scope is enforced in code; handing the autonomous dream
a full-definition editor would void that guarantee.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import ObjectSchema, StringSchema, tool_parameters_schema

_PARAMETERS = tool_parameters_schema(
    name=StringSchema("Name of the EXISTING workflow to edit."),
    definition=ObjectSchema(
        description=(
            "The full replacement definition (the complete flow graph — not a "
            "patch). Load the current one first (workflows/<name>.json or the "
            "list_workflows I/O), apply your change, and pass the whole result."
        ),
        additional_properties=True,
    ),
    rationale=StringSchema(
        "Why this edit — recorded in the version history (the improvement pass "
        "reads it to avoid re-proposing reverted changes)."
    ),
    required=["name", "definition", "rationale"],
    description=(
        "Edit an EXISTING workflow definition (create new ones with "
        "`workflow_write`). The full replacement definition is validated as a "
        "graph before it is saved — schema errors come back verbatim — and the "
        "change is committed to the workflow version history. Use for a "
        "user-requested change to a workflow's nodes, prompts, routing, or I/O."
    ),
)


@tool_parameters(_PARAMETERS)
class WorkflowEditTool(Tool):
    """workflow_edit tool — validate and persist an edit to an existing workflow."""

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "workflow_edit"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> "WorkflowEditTool":
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> str:
        from durin.workflow.editing import save_workflow_definition

        result = save_workflow_definition(
            self._workspace,
            str(kwargs.get("name", "")),
            kwargs.get("definition"),
            reason=str(kwargs.get("rationale", "")),
            actor="agent",
            must_exist=True,
        )
        return json.dumps(result)
