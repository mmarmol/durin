"""workflow_script_write tool — the sanctioned door for workflow script files.

``workflows/scripts/`` holds the code a script node executes. It used to be
reachable only through the generic file tools, which validate nothing and leave
the workflow version store dirty; now that the write guard denies ``workflows/``
to those tools, this is how an agent authors a script. Persists through the
shared editing engine (validate → atomic write under the editor's lock →
version-store commit), like ``workflow_write`` does for definitions.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

_PARAMETERS = tool_parameters_schema(
    name=StringSchema(
        "Script file name — a single path segment, e.g. 'resolve-org.py'. It is "
        "written under <workspace>/workflows/scripts/."
    ),
    content=StringSchema("Full file content. Replaces the file when it already exists."),
    rationale=StringSchema(
        "Why this script is being written — recorded in the version history."
    ),
    required=["name", "content", "rationale"],
    description=(
        "Create or replace a workflow script file (the code a `script` node runs). "
        "Use when a workflow needs a deterministic step: author the script here and "
        "point a script node at it by file name. The generic file tools cannot write "
        "under workflows/ — this door validates the name, writes atomically under the "
        "editor's lock, and commits the change to the workflow version history."
    ),
)


@tool_parameters(_PARAMETERS)
class WorkflowScriptWriteTool(Tool):
    """workflow_script_write tool — validate and persist a workflow script file."""

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "workflow_script_write"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @classmethod
    def create(cls, ctx: Any) -> "WorkflowScriptWriteTool":
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> str:
        from durin.workflow.editing import save_workflow_script

        result = save_workflow_script(
            self._workspace,
            str(kwargs.get("name", "")),
            str(kwargs.get("content", "")),
            reason=str(kwargs.get("rationale", "")),
            actor="agent",
        )
        if result.get("ok"):
            result["note"] = "reference it from a script node by this file name"
        return json.dumps(result)
