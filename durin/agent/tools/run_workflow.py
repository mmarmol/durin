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
import uuid
from contextvars import ContextVar
from typing import Any

from durin.agent.tools.base import Tool, tool_parameters
from durin.agent.tools.context import ContextAware, RequestContext


def _terminal_progress_payload(workflow: Any, run_id: str, runs: Any) -> dict:
    """Build the final ``workflow_progress`` payload (``done=True``) from the
    completed node runs.

    The engine only emits per-node ``done=False`` frames during the walk and
    never a terminal frame, so without this the WORK panel (TUI + webui) leaves
    a finished workflow stuck on "running". Emitted after the run completes,
    keyed by the same ``run_id`` so it updates the existing work item.
    """
    from durin.workflow.spec import node_label

    nodes = [
        {
            "id": r.node_id,
            "label": node_label(workflow.nodes[r.node_id]) if r.node_id in workflow.nodes else r.node_id,
            "status": "failed" if r.status in ("node_failed", "persist_failed") else "done",
            "route_label": getattr(r, "route_label", None),
            "iteration": r.iteration,
            "budget": getattr(r, "budget", None),
        }
        for r in runs
    ]
    return {"run_id": run_id, "nodes": nodes, "done": True}

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
        "input_files": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Optional. Absolute paths of files to hand the workflow as input. Each is copied "
                "into the run's shared working folder before the start node runs, so every node "
                "(including tool-less ones, and a dynamic fan-out of one worker per file) can read "
                "them there. Use this instead of pasting file contents into 'task'."
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
        "resume_run_id": {
            "type": "string",
            "description": (
                "Optional. The run_id of a prior run of THIS workflow that ended asking for "
                "more information (needs_input). Pass the user's answers as 'task': the run "
                "resumes at the node that asked — same working folder, sessions and visit "
                "counts — instead of restarting from scratch."
            ),
        },
    },
    "required": ["name", "task"],
}


def _background_launch_message(name: str, run_id: str) -> str:
    """The reply a background launch returns to the agent. It carries the run id and
    points at the `tasks` tool — so the agent learns, at launch, that it can observe
    or cancel the run rather than only wait for the follow-up."""
    return (
        f"Workflow '{name}' started in the background (run id: {run_id}). "
        "You can keep working; I'll deliver its result as a follow-up when it "
        f"finishes. To check progress meanwhile call tasks(action='status', "
        f"id='{run_id}'), or tasks(action='stop', id='{run_id}') to cancel it."
    )


def _format_result(result: Any, output_files: bool = False) -> str:
    lines = [f"Workflow run {result.run_id}: {result.status}"]

    if result.status == "needs_input":
        lines.append(
            "The workflow needs more information before it can finish — it did NOT fail. "
            "You own the conversation with the user, so ask them the questions below "
            "(via ask_user_question or just in your reply), then call this workflow again."
        )
        if result.final_output:
            lines.append(f"\nNeeds clarification:\n{result.final_output}")
        # A needs_input result without a needs_input_node has no manifest to resume from
        # (e.g. an engine pre-flight check) — only offer resume when one is set.
        if result.needs_input_node:
            lines.append(
                f"\nTo continue after the user answers, call run_workflow again with "
                f"resume_run_id='{result.run_id}' and the answers as the task."
            )
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

    # Surface the run's working folder only when the workflow declares it outputs files,
    # so a file-producing run tells the agent where to read its outputs. The folder also
    # holds any seeded input files. Pure-text workflows stay silent (no noise).
    if output_files and result.output_dir:
        lines.append(f"\nThe workflow's output files are in: {result.output_dir}")
        for rel in (result.output_files or [])[:20]:
            lines.append(f"  - {rel}")
        overflow = len(result.output_files or []) - 20
        if overflow > 0:
            lines.append(f"  … and {overflow} more")
        lines.append("Copy out any deliverable you need to keep: this folder is pruned "
                     "after newer runs accumulate.")

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
            "Returns a run summary. Pass input_files (absolute paths) to hand the workflow files "
            "to work on — they are placed in the run's shared working folder for every node to read. "
            "If the summary says the workflow needs more information, "
            "it ENDED asking for clarification (it did not fail) — ask the user those questions "
            "and call this tool again with resume_run_id set to the run's id and the user's "
            "answers as the task. "
            "Pass background=false to block and get the result inline only when you need it "
            "to continue right now; otherwise it runs in the background and its result is "
            "delivered as a follow-up message. A background launch returns the run's id; "
            "use tasks(action='status', id=...) to check progress or tasks(action='stop', "
            "id=...) to cancel it."
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
            "information, ask the user those questions and re-run the workflow with "
            "resume_run_id set to the run's id and their answers as the task."
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

    async def execute(self, name: str, task: str, output_format: str = "", input_files: list[str] | None = None, background: bool = True, resume_run_id: str = "") -> str:  # type: ignore[override]
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

        resume = None
        if resume_run_id:
            from durin.workflow import run_log
            from durin.workflow.engine import build_resume_state
            manifest = run_log.read_manifest(self._workspace, name, resume_run_id)
            if manifest is None:
                return f"Error: no run '{resume_run_id}' recorded for workflow '{name}'."
            if manifest.get("status") != "needs_input" or not manifest.get("needs_input_node"):
                return (f"Error: run '{resume_run_id}' has status "
                        f"'{manifest.get('status')}' and cannot be resumed — only a "
                        f"needs_input run can.")
            resume = build_resume_state(manifest, task)
            task = manifest.get("task") or task

        # Snapshot the current definitions into the workflow version history (captures
        # any edit since the last run). Best-effort: never blocks the run.
        WorkflowVersionStore(workflows_dir(self._workspace)).snapshot(f"run {name}")

        # Whether this workflow declares it produces files — gates surfacing the run's
        # working folder in the summary so a file-producing run points the agent at its outputs.
        output_files = bool((workflow.output or {}).get("file"))

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
        # Pre-generate the run id (instead of letting the engine mint it) for two
        # reasons: the terminal "done" progress frame emitted after the run must
        # carry the same id to update the existing work item, and a background
        # launch returns it so the agent can poll/cancel via the `tasks` tool —
        # the engine's cooperative cancel is keyed by it.
        from durin.workflow.cancellation import clear as _clear_cancel
        from durin.workflow.cancellation import is_cancelled as _is_cancelled
        run_id = resume.run_id if resume is not None else uuid.uuid4().hex[:12]
        engine = WorkflowEngine(
            node_runner=node_runner,
            run_id_factory=lambda: run_id,
            subworkflow_runner=subworkflow_runner,
            workspace=self._workspace,
            pick_runner=judge_runner.pick,
            max_node_visits=self._app_config.workflow.max_node_visits,
            progress_emit=progress_emit,
            cancel_check=lambda: _is_cancelled(run_id),
            prune_keep=self._app_config.workflow.keep_runs,
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
                        input_files=input_files or None,
                        output_format=output_format or None,
                        resume=resume,
                    )
                    if progress_emit is not None:
                        progress_emit(_terminal_progress_payload(workflow, run_id, result.runs))
                    summary = _format_result(result, output_files=output_files)
                except Exception as exc:  # noqa: BLE001
                    # Clear the work item even on failure so it doesn't hang on "running".
                    if progress_emit is not None:
                        progress_emit({"run_id": run_id, "nodes": [], "done": True})
                    summary = f"Workflow run failed in background: {exc}"
                finally:
                    # Drop the cancel flag now the run is over (whether or not it was set).
                    _clear_cancel(run_id)
                await self._inject_result(summary, name=name, inject_target=inject_target)

            task_handle = asyncio.create_task(_run_and_inject())
            # Keep a reference so the task isn't garbage-collected mid-flight.
            self._bg_tasks.add(task_handle)
            task_handle.add_done_callback(self._bg_tasks.discard)
            return _background_launch_message(name, run_id)

        # The engine owns the run manifest (started→updated→finalized); no record write here.
        result = await asyncio.to_thread(
            engine.run, workflow, task,
            root_session_key=root_session_key,
            input_files=input_files or None,
            output_format=output_format or None,
            resume=resume,
        )
        if progress_emit is not None:
            progress_emit(_terminal_progress_payload(workflow, run_id, result.runs))
        return _format_result(result, output_files=output_files)
