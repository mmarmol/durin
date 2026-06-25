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
from contextvars import ContextVar
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext

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

    if result.status != "completed":
        if result.status == "exhausted" and result.exhausted_node:
            lines.append(
                f"The workflow did not complete: node '{result.exhausted_node}' was retried "
                "up to its limit and its check kept failing."
            )
            last_fail = max(
                (r for r in result.runs if r.node_id == result.exhausted_node and r.passed is False),
                key=lambda r: r.iteration,
                default=None,
            )
            if last_fail is not None:
                lines.append(f"Last failure reason:\n{last_fail.output}")
        else:
            lines.append(f"The workflow did not complete (status: {result.status}).")

        if result.final_output:
            lines.append(f"Closest result:\n{result.final_output}")

    for r in result.runs:
        if r.passed is not None:
            # A routing/decision node: surface its verdict and, when it ran as an agent
            # turn (so the verdict is auditable), the session key. A command gate has no
            # session and renders the verdict alone.
            line = f"  [{r.node_id}#{r.iteration}] decision: {'pass' if r.passed else 'fail'}"
            if r.session_key:
                line += f" -> {r.session_key}"
            lines.append(line)
        else:
            lines.append(f"  [{r.node_id}#{r.iteration}] -> {r.session_key or '(no session)'}")

    if result.status == "completed" and result.final_output:
        lines.append(f"\nFinal output:\n{result.final_output}")

    return "\n".join(lines)


@tool_parameters(_PARAMETERS)
class RunWorkflowTool(Tool, ContextAware):
    """Run a user-defined workflow (a flow graph of nodes) on a task."""

    def __init__(self, workspace: str, sessions: Any, app_config: Any, live_tool_registry: Any = None) -> None:
        self._workspace = workspace
        self._sessions = sessions
        self._app_config = app_config
        self._live_tool_registry = live_tool_registry
        self._session_key: ContextVar[str | None] = ContextVar("run_workflow_session_key", default=None)

    def set_context(self, ctx: RequestContext) -> None:
        self._session_key.set(ctx.session_key)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None and getattr(ctx, "app_config", None) is not None

    @classmethod
    def create(cls, ctx: Any) -> "RunWorkflowTool":
        return cls(
            workspace=ctx.workspace, sessions=ctx.sessions, app_config=ctx.app_config,
            live_tool_registry=getattr(ctx, "live_tool_registry", None),
        )

    @property
    def name(self) -> str:
        return "run_workflow"

    @property
    def description(self) -> str:
        return (
            "Run a user-defined workflow on a task. The workflow is a flow graph of "
            "nodes (defined in <workspace>/workflows/<name>.json); a node does the work "
            "and, when it has routing set (on_pass/on_fail), routes the flow on its "
            "verdict. Returns a run summary."
        )

    async def execute(self, name: str, task: str) -> str:  # type: ignore[override]
        from durin.agent.runner import AgentRunner
        from durin.providers.factory import make_provider
        from durin.workflow.engine import WorkflowEngine
        from durin.workflow.judge import AgentJudgeRunner
        from durin.workflow.loader import WorkflowNotFound, load_workflow, workflows_dir
        from durin.workflow.node_runner import AgentNodeRunner
        from durin.workflow.subworkflow import SubworkflowRunner
        from durin.workflow.version_store import WorkflowVersionStore

        try:
            workflow = load_workflow(self._workspace, name)
        except WorkflowNotFound as exc:
            return f"Error: {exc}"

        # Snapshot the current definitions into the workflow version history (captures
        # any edit since the last run). Best-effort: never blocks the run.
        WorkflowVersionStore(workflows_dir(self._workspace)).snapshot(f"run {name}")

        preset = self._app_config.resolve_default_preset()
        provider = make_provider(self._app_config, preset=preset)
        runner = AgentRunner(provider)
        # The MCP sessions live on this (the gateway's) event loop; the engine runs
        # in a worker thread, so node MCP calls are marshalled back here.
        main_loop = asyncio.get_running_loop()
        node_runner = AgentNodeRunner(
            runner, self._sessions, default_model=provider.get_default_model(),
            tools_config=self._app_config.tools,
            live_tool_registry=self._live_tool_registry,
            main_loop=main_loop,
            app_config=self._app_config,
        )
        judge_runner = AgentJudgeRunner(runner, default_model=provider.get_default_model())
        subworkflow_runner = SubworkflowRunner(self._workspace, node_runner, judge_runner)
        engine = WorkflowEngine(
            node_runner=node_runner,
            command_cwd=self._workspace,
            subworkflow_runner=subworkflow_runner,
            workspace=self._workspace,
            pick_runner=judge_runner.pick,
            max_node_visits=self._app_config.workflow.max_node_visits,
        )
        root_session_key = self._session_key.get()
        result = await asyncio.to_thread(engine.run, workflow, task, root_session_key=root_session_key)
        # The engine owns the run manifest (started→updated→finalized); no record write here.
        return _format_result(result)
