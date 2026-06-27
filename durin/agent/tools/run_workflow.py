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
        "output_format": {
            "type": "string",
            "description": (
                "Optional. How you want the result delivered THIS run — form, content, or "
                "length (e.g. 'a bulleted list', 'JSON with fields title,summary', 'a 3-line "
                "summary'). Overrides the workflow's default output contract for this call only. "
                "Omit to use the workflow's own output description."
            ),
        },
        "background": {
            "type": "boolean",
            "description": (
                "Optional (default TRUE — runs in the BACKGROUND). A background run returns "
                "immediately and you keep working; the workflow's result is delivered to you "
                "as a follow-up message when it finishes. Pass background=false ONLY when you "
                "need the result to keep reasoning in THIS turn (it then blocks and returns the "
                "result directly). Default to background so the chat is never blocked."
            ),
        },
    },
    "required": ["name", "task"],
}


def _format_result(result: Any) -> str:
    lines = [f"Workflow run {result.run_id}: {result.status}"]

    if result.status == "needs_input":
        lines.append(
            "The workflow needs more information before it can finish — it did NOT fail. "
            "You own the conversation with the user, so ask them the questions below "
            "(via ask_user_question or just in your reply), then call this workflow again "
            "with the SAME task plus the user's answers appended."
        )
        if result.final_output:
            lines.append(f"\nNeeds clarification:\n{result.final_output}")
    elif result.status != "completed":
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

    def __init__(self, workspace: str, sessions: Any, app_config: Any, live_tool_registry: Any = None, bus: Any = None) -> None:
        self._workspace = workspace
        self._sessions = sessions
        self._app_config = app_config
        self._live_tool_registry = live_tool_registry
        self._bus = bus
        self._session_key: ContextVar[str | None] = ContextVar("run_workflow_session_key", default=None)
        self._chat_id: ContextVar[str | None] = ContextVar("run_workflow_chat_id", default=None)
        self._channel: ContextVar[str | None] = ContextVar("run_workflow_channel", default=None)
        # Strong-reference set so background tasks aren't GC'd mid-flight.
        self._bg_tasks: set = set()

    def set_context(self, ctx: RequestContext) -> None:
        self._session_key.set(ctx.session_key)
        self._chat_id.set(ctx.chat_id)
        self._channel.set(ctx.channel)

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        return getattr(ctx, "sessions", None) is not None and getattr(ctx, "app_config", None) is not None

    @classmethod
    def create(cls, ctx: Any) -> "RunWorkflowTool":
        return cls(
            workspace=ctx.workspace, sessions=ctx.sessions, app_config=ctx.app_config,
            live_tool_registry=getattr(ctx, "live_tool_registry", None),
            bus=getattr(ctx, "bus", None),
        )

    @property
    def name(self) -> str:
        return "run_workflow"

    @property
    def description(self) -> str:
        return (
            "Run a user-defined workflow on a task. The workflow is a flow graph of "
            "nodes (defined in <workspace>/workflows/<name>.json); a node does the work "
            "and, when it has routing set (on_pass/on_fail), routes the flow on its verdict. "
            "Returns a run summary. If the summary says the workflow needs more information, "
            "it ENDED asking for clarification (it did not fail) — ask the user those questions "
            "and call this tool again with the same task plus their answers. "
            "Pass background=false to block and get the result inline only when you need it "
            "to continue right now; otherwise it runs in the background and its result is "
            "delivered as a follow-up message."
        )

    async def _inject_result(self, summary: str, *, name: str, inject_target: dict) -> None:
        """Inject a finished background-workflow result back to the agent.

        Mirrors SubagentManager._announce_result: a system InboundMessage whose
        session_key_override routes it into the parent session's pending queue so
        the agent picks it up mid-turn (or as its next turn) and acts on it —
        including a needs_input result, which carries its own ask-and-re-run guidance.
        """
        if self._bus is None:
            return
        from durin.bus.events import InboundMessage
        channel = inject_target.get("channel") or "websocket"
        chat_id = inject_target.get("chat_id") or ""
        override = inject_target.get("session_key") or f"{channel}:{chat_id}"
        content = (
            f"[Background workflow '{name}' finished]\n\n{summary}\n\n"
            "Summarize the outcome for the user. If it says the workflow needs more "
            "information, ask the user those questions and re-run the workflow with the "
            "same task plus their answers."
        )
        msg = InboundMessage(
            channel="system",
            sender_id="workflow_background",
            chat_id=f"{channel}:{chat_id}",
            content=content,
            session_key_override=override,
            metadata={"injected_event": "workflow_background_result", "workflow": name},
        )
        try:
            await self._bus.publish_inbound(msg)
        except Exception:  # noqa: BLE001 - best-effort; the run already persisted its manifest
            pass

    async def execute(self, name: str, task: str, output_format: str = "", background: bool = True) -> str:  # type: ignore[override]
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
        bus = self._bus
        run_chat_id = self._chat_id.get()
        progress_emit = None
        if bus is not None and run_chat_id is not None:
            def _emit_progress(payload: dict) -> None:
                from durin.bus.events import OutboundMessage
                ev = {
                    "version": 1,
                    "phase": "end" if payload.get("done") else "running",
                    "call_id": f"workflow:{payload['run_id']}",
                    "name": "workflow_progress",
                    "arguments": {"workflow": name, "task": task},
                    "nodes": payload["nodes"],
                }
                try:
                    asyncio.run_coroutine_threadsafe(
                        bus.publish_outbound(OutboundMessage(
                            channel="websocket",
                            chat_id=run_chat_id,
                            content="",
                            metadata={"_progress": True, "_tool_hint": True, "_tool_events": [ev]},
                        )),
                        main_loop,
                    )
                except Exception:  # noqa: BLE001 - best-effort; never break the run
                    pass
            progress_emit = _emit_progress
        engine = WorkflowEngine(
            node_runner=node_runner,
            subworkflow_runner=subworkflow_runner,
            workspace=self._workspace,
            pick_runner=judge_runner.pick,
            max_node_visits=self._app_config.workflow.max_node_visits,
            progress_emit=progress_emit,
        )
        root_session_key = self._session_key.get()
        inject_target = {
            "channel": self._channel.get() or "websocket",
            "chat_id": self._chat_id.get() or "",
            "session_key": root_session_key,
        }

        if background:
            async def _run_and_inject() -> None:
                try:
                    result = await asyncio.to_thread(
                        engine.run, workflow, task,
                        root_session_key=root_session_key,
                        output_format=output_format or None,
                    )
                    summary = _format_result(result)
                except Exception as exc:  # noqa: BLE001
                    summary = f"Workflow run failed in background: {exc}"
                await self._inject_result(summary, name=name, inject_target=inject_target)

            task_handle = asyncio.create_task(_run_and_inject())
            # Keep a reference so the task isn't garbage-collected mid-flight.
            self._bg_tasks.add(task_handle)
            task_handle.add_done_callback(self._bg_tasks.discard)
            return (
                f"Workflow '{name}' started in the background. You can keep working; "
                "I'll deliver its result to you as a follow-up when it finishes."
            )

        # The engine owns the run manifest (started→updated→finalized); no record write here.
        result = await asyncio.to_thread(
            engine.run, workflow, task,
            root_session_key=root_session_key,
            output_format=output_format or None,
        )
        return _format_result(result)
