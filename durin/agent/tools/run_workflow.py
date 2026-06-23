"""Agent tool: run a user-defined workflow on a task.

Loads ``<workspace>/workflows/<name>.json``, builds the workflow engine wired to a
node runner (which runs each work node as a real agent turn with that node's tools
and model), runs it, and returns a result summary. The tool's ``execute`` is async
but the engine is synchronous and its node runner calls ``asyncio.run`` internally,
so the engine is driven via ``asyncio.to_thread`` — the inner ``asyncio.run`` then
runs in a worker thread with no active loop, which is valid.
"""

from __future__ import annotations

import asyncio
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters

_PARAMETERS = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": "Name of the workflow to run — a <name>.json file under the workspace's 'workflows' directory.",
        },
        "task": {
            "type": "string",
            "description": "The task / input to run through the workflow.",
        },
    },
    "required": ["name", "task"],
}


def _format_result(result: Any) -> str:
    lines = [f"Workflow run {result.run_id}: {result.status}"]
    for r in result.runs:
        if r.passed is not None:
            lines.append(f"  [{r.node_id}#{r.iteration}] decision: {'pass' if r.passed else 'fail'}")
        else:
            lines.append(f"  [{r.node_id}#{r.iteration}] -> {r.session_key or '(no session)'}")
    if result.final_output:
        lines.append(f"\nFinal output:\n{result.final_output}")
    return "\n".join(lines)


@tool_parameters(_PARAMETERS)
class RunWorkflowTool(Tool):
    """Run a user-defined workflow (a flow graph of nodes) on a task."""

    def __init__(self, workspace: str, sessions: Any, app_config: Any) -> None:
        self._workspace = workspace
        self._sessions = sessions
        self._app_config = app_config

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None and getattr(ctx, "app_config", None) is not None

    @classmethod
    def create(cls, ctx: Any) -> "RunWorkflowTool":
        return cls(workspace=ctx.workspace, sessions=ctx.sessions, app_config=ctx.app_config)

    @property
    def name(self) -> str:
        return "run_workflow"

    @property
    def description(self) -> str:
        return (
            "Run a user-defined workflow on a task. The workflow is a flow graph of "
            "nodes (defined in <workspace>/workflows/<name>.json); work nodes do the "
            "work and decision nodes route the flow. Returns a run summary."
        )

    async def execute(self, name: str, task: str) -> str:  # type: ignore[override]
        from durin.agent.runner import AgentRunner
        from durin.providers.factory import make_provider
        from durin.workflow.engine import WorkflowEngine
        from durin.workflow.loader import WorkflowNotFound, load_workflow
        from durin.workflow.node_runner import AgentNodeRunner

        try:
            workflow = load_workflow(self._workspace, name)
        except WorkflowNotFound as exc:
            return f"Error: {exc}"

        preset = self._app_config.resolve_default_preset()
        provider = make_provider(self._app_config, preset=preset)
        runner = AgentRunner(provider)
        node_runner = AgentNodeRunner(
            runner, self._sessions, default_model=provider.get_default_model()
        )
        engine = WorkflowEngine(node_runner=node_runner)
        result = await asyncio.to_thread(engine.run, workflow, task)
        return _format_result(result)
