"""list_workflows tool — list the workflows defined in this workspace.

Auto-discovered into the agent's `core` toolset (like run_workflow). Discovery-only:
it reads the LOCAL definitions under ``<workspace>/workflows/<name>.json`` and returns
each workflow's name, description, and I/O so the agent knows which one to run with
``run_workflow``. It NEVER runs a workflow — that is run_workflow's job.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.schema import StringSchema, tool_parameters_schema

_PARAMETERS = tool_parameters_schema(
    query=StringSchema(
        "Optional substring to filter by name/description."
    ),
    description=(
        "List the workflows available in this workspace — each with what it does and its "
        "input/output — so you know which to run with `run_workflow`. Optional `query` "
        "filters by name or description."
    ),
)


@tool_parameters(_PARAMETERS)
class ListWorkflowsTool(Tool):
    """list_workflows tool — list the workspace's local workflow definitions."""

    def __init__(self, workspace: str | Path) -> None:
        self._workspace = Path(workspace).expanduser()

    @property
    def name(self) -> str:
        return "list_workflows"

    @property
    def description(self) -> str:
        return _PARAMETERS["description"]

    @property
    def read_only(self) -> bool:
        return True

    @classmethod
    def create(cls, ctx: Any) -> "ListWorkflowsTool":
        return cls(workspace=ctx.workspace)

    async def execute(self, **kwargs: Any) -> Any:
        from durin.workflow.loader import load_workflow, workflows_dir

        d = workflows_dir(self._workspace)
        q = str(kwargs.get("query", "")).strip().lower()
        out = []
        if d.is_dir():
            for f in sorted(d.glob("*.json")):
                try:
                    wf = load_workflow(self._workspace, f.stem)
                except Exception:   # noqa: BLE001 - skip a malformed/unparseable def
                    continue
                if q and q not in f.stem.lower() and q not in (wf.description or "").lower():
                    continue
                out.append({"name": wf.name, "description": wf.description,
                            "input": wf.input, "output": wf.output,
                            "improvement_mode": wf.improvement_mode})
        return {"workflows": out, "note": "run one with run_workflow(name, task)"}
