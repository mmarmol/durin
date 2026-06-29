"""Subagent manager for background task execution."""

import asyncio
import json
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from loguru import logger

from durin.agent.hook import AgentHook, AgentHookContext
from durin.agent.runner import AgentRunner, AgentRunResult, AgentRunSpec
from durin.agent.tools.context import ToolContext
from durin.agent.tools.file_state import FileStates
from durin.agent.tools.loader import ToolLoader
from durin.agent.tools.registry import ToolRegistry
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.config.schema import AgentDefaults, ToolsConfig
from durin.providers.base import LLMProvider
from durin.utils.prompt_templates import render_template


@dataclass(slots=True)
class SubagentStatus:
    """Real-time status of a running OR recently-completed subagent.

    Lives in ``SubagentManager._task_statuses`` for the lifetime of the
    task plus until the LRU window evicts it (default 100 statuses). The
    ``session_key`` and ``final_content`` fields are populated so the
    lifecycle tools can answer questions like "what did subagent X
    return?" without having to scan ``session.messages``.
    """

    task_id: str
    label: str
    task_description: str
    started_at: float          # time.monotonic()
    phase: str = "initializing"  # initializing | awaiting_tools | tools_completed | final_response | done | error
    iteration: int = 0
    tool_events: list = field(default_factory=list)   # [{name, status, detail}, ...]
    usage: dict = field(default_factory=dict)          # token usage
    stop_reason: str | None = None
    error: str | None = None
    session_key: str | None = None
    final_content: str | None = None
    ended_at: float | None = None    # time.monotonic() when the task finished


class _SubagentHook(AgentHook):
    """Hook for subagent execution — logs tool calls and updates status."""

    def __init__(
        self,
        task_id: str,
        status: SubagentStatus | None = None,
        *,
        bus: Any | None = None,
        origin: dict | None = None,
    ) -> None:
        super().__init__()
        self._task_id = task_id
        self._status = status
        self._bus = bus
        self._origin = origin or {}

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        for tool_call in context.tool_calls:
            args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
            logger.debug(
                "Subagent [{}] executing: {} with arguments: {}",
                self._task_id, tool_call.name, args_str,
            )

    async def after_iteration(self, context: AgentHookContext) -> None:
        if self._status is None:
            return
        self._status.iteration = context.iteration
        self._status.tool_events = list(context.tool_events)
        self._status.usage = dict(context.usage)
        if context.error:
            self._status.error = str(context.error)
        # Live progress: emit a running frame the webui merges into the
        # sub-agent block by call_id (same call_id/name as the final result).
        # Best-effort: a publish failure must never break the sub-agent run.
        if self._bus is not None and self._origin.get("chat_id"):
            try:
                from durin.bus.events import OutboundMessage
                last_tool = None
                if context.tool_events:
                    te = context.tool_events[-1]
                    last_tool = te.get("name") if isinstance(te, dict) else None
                ev = {
                    "version": 1,
                    "phase": "running",
                    "call_id": f"subagent:{self._task_id}",
                    "name": "subagent_result",
                    "arguments": {
                        "label": self._status.label,
                        "task": self._status.task_description,
                    },
                    "progress": {"iteration": context.iteration, "tool": last_tool},
                }
                await self._bus.publish_outbound(OutboundMessage(
                    channel=self._origin["channel"],
                    chat_id=self._origin["chat_id"],
                    content="",
                    metadata={"_progress": True, "_tool_hint": True, "_tool_events": [ev]},
                ))
            except Exception:
                logger.debug("Subagent [{}] progress emit failed (suppressed)", self._task_id)


class SubagentManager:
    """Manages background subagent execution."""

    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        max_tool_result_chars: int,
        model: str | None = None,
        tools_config: ToolsConfig | None = None,
        restrict_to_workspace: bool = False,
        disabled_skills: list[str] | None = None,
        max_iterations: int | None = None,
        llm_wall_timeout_for_session: Callable[[str | None], float | None] | None = None,
        sessions: Any | None = None,
    ):
        defaults = AgentDefaults()
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        # Optional SessionManager — when provided, subagent inherits the
        # parent session's agent_mode. Without it, falls back to a static
        # EXPLORE_MODE (the safe-but-stricter default).
        self._sessions = sessions
        self.model = model or provider.get_default_model()
        self.tools_config = tools_config or ToolsConfig()
        self.max_tool_result_chars = max_tool_result_chars
        self.restrict_to_workspace = restrict_to_workspace
        self.disabled_skills = set(disabled_skills or [])
        self.max_iterations = (
            max_iterations
            if max_iterations is not None
            else defaults.max_tool_iterations
        )
        self.max_concurrent_subagents = defaults.max_concurrent_subagents
        self.runner = AgentRunner(provider)
        self._llm_wall_timeout_for_session = llm_wall_timeout_for_session
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._task_statuses: dict[str, SubagentStatus] = {}
        # ``_session_tasks`` retains task ids for completed subagents too,
        # so ``list_for_session`` can include recent history. Trimming
        # happens lazily in ``_remember_finished``.
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> {task_id, ...}
        self._max_status_history = 100

    def _subagent_tools_config(self) -> ToolsConfig:
        """Build a ToolsConfig scoped for subagent use."""
        return ToolsConfig(
            exec=self.tools_config.exec,
            web=self.tools_config.web,
            restrict_to_workspace=self.restrict_to_workspace,
        )

    def _build_tools(
        self,
        workspace: Path | None = None,
        tools_config: ToolsConfig | None = None,
    ) -> ToolRegistry:
        """Build an isolated subagent tool registry via ToolLoader."""
        root = self.workspace if workspace is None else workspace
        registry = ToolRegistry()
        cfg = tools_config if tools_config is not None else self._subagent_tools_config()
        ctx = ToolContext(
            config=cfg,
            workspace=str(root.resolve()),
            file_state_store=FileStates(),
            scope="subagent",
        )
        ToolLoader().load(ctx, registry, scope="subagent")
        return registry

    def set_provider(self, provider: LLMProvider, model: str) -> None:
        self.provider = provider
        self.model = model
        self.runner.provider = provider

    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        origin_message_id: str | None = None,
    ) -> str:
        """Spawn a subagent to execute a task in the background."""
        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")
        origin = {"channel": origin_channel, "chat_id": origin_chat_id, "session_key": session_key}

        status = SubagentStatus(
            task_id=task_id,
            label=display_label,
            task_description=task,
            started_at=time.monotonic(),
            session_key=session_key,
        )
        self._task_statuses[task_id] = status

        bg_task = asyncio.create_task(
            self._run_subagent(task_id, task, display_label, origin, status, origin_message_id)
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task) -> None:
            # Drop only the asyncio.Task handle. The SubagentStatus stays
            # in ``_task_statuses`` so the lifecycle tools can still report
            # the final phase / output. The status is trimmed lazily via
            # ``_remember_finished`` once we exceed the history window.
            self._running_tasks.pop(task_id, None)
            self._remember_finished(task_id)

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent [{}]: {}", task_id, display_label)
        return (
            f"Subagent [{display_label}] started (id: {task_id}). "
            "I'll notify you when it completes. To check on it meanwhile call "
            f"tasks(action='status', id='{task_id}'), or tasks(action='stop', "
            f"id='{task_id}') to cancel it.\n\n"
            "IMPORTANT: subagents always run in EXPLORE MODE (read-only). "
            "The subagent CAN: read_file, list_dir, grep, repo_overview, "
            "web_fetch, web_search. The subagent CANNOT: edit_file, "
            "write_file, exec, or any state-changing tool. "
            "If your task requires modifications, do them yourself "
            "(when you're in build mode) or adjust the subagent's task to "
            "investigation only. If you are in PLAN MODE, neither you nor "
            "the subagent can modify — call exit_plan_mode(plan=...) and "
            "wait for the user to /build."
        )

    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
        status: SubagentStatus,
        origin_message_id: str | None = None,
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent [{}] starting task: {}", task_id, label)

        async def _on_checkpoint(payload: dict) -> None:
            status.phase = payload.get("phase", status.phase)
            status.iteration = payload.get("iteration", status.iteration)

        try:
            tools = self._build_tools()
            system_prompt = self._build_subagent_prompt()
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            sess_key = origin.get("session_key")
            llm_timeout = (
                self._llm_wall_timeout_for_session(sess_key)
                if self._llm_wall_timeout_for_session
                else None
            )
            # Subagent mode = parent's mode when known, else EXPLORE.
            # If parent is in plan mode, the subagent is also in plan —
            # both restricted to read-only + exit_plan_mode. The model
            # understands delegation does not escape the mode, so it
            # doesn't fall into "spawn to work around restrictions".
            # Without a SessionManager handle we fall back to EXPLORE —
            # the safer-but-stricter default.
            from durin.agent.agent_mode import EXPLORE_MODE, get_active_mode

            def _subagent_mode_provider():
                if self._sessions is not None and sess_key:
                    try:
                        parent_session = self._sessions.get_or_create(sess_key)
                        return get_active_mode(parent_session)
                    except Exception:
                        pass
                return EXPLORE_MODE

            # Capture provider snapshot at spec-build time: a concurrent
            # session's /model
            # swap calls set_provider(), mutating self.runner.provider. Pinning
            # the provider here makes this in-flight subagent turn immune to
            # that mutation, symmetric with the AgentLoop fix.
            subagent_provider = self.runner.provider
            result = await self.runner.run(AgentRunSpec(
                initial_messages=messages,
                tools=tools,
                model=self.model,
                provider=subagent_provider,
                max_iterations=self.max_iterations,
                max_tool_result_chars=self.max_tool_result_chars,
                hook=_SubagentHook(task_id, status, bus=self.bus, origin=origin),
                max_iterations_message="Task completed but no final response was generated.",
                error_message=None,
                fail_on_tool_error=True,
                checkpoint_callback=_on_checkpoint,
                session_key=sess_key,
                llm_timeout_s=llm_timeout,
                mode_provider=_subagent_mode_provider,
            ))
            status.phase = "done"
            status.stop_reason = result.stop_reason
            status.ended_at = time.monotonic()
            self._persist_subagent_session(task_id, sess_key, result, label)

            if result.stop_reason == "tool_error":
                status.tool_events = list(result.tool_events)
                partial = self._format_partial_progress(result)
                status.final_content = partial
                await self._announce_result(
                    task_id, label, task, partial,
                    origin, "error", origin_message_id,
                )
            elif result.stop_reason == "error":
                err_text = result.error or "Error: subagent execution failed."
                status.final_content = err_text
                await self._announce_result(
                    task_id, label, task, err_text,
                    origin, "error", origin_message_id,
                )
            else:
                final_result = result.final_content or "Task completed but no final response was generated."
                status.final_content = final_result
                logger.info("Subagent [{}] completed successfully", task_id)
                await self._announce_result(task_id, label, task, final_result, origin, "ok", origin_message_id)

        except Exception as e:
            status.phase = "error"
            status.error = str(e)
            status.ended_at = time.monotonic()
            status.final_content = f"Error: {e}"
            logger.exception("Subagent [{}] failed", task_id)
            await self._announce_result(task_id, label, task, f"Error: {e}", origin, "error", origin_message_id)

    def _persist_subagent_session(
        self, task_id: str, parent_key: str | None, result: AgentRunResult, label: str
    ) -> None:
        """Persist a finished subagent's conversation as its own session,
        linked to the parent so the work is navigable, searchable, and not
        lost when the in-memory status LRU evicts it.

        No-op when no SessionManager is wired or there is no parent key.
        """
        if self._sessions is None or not parent_key:
            return
        try:
            from durin.session.lineage import build_lineage, root_of
            from durin.session.manager import Session

            parent_meta = self._sessions.get_or_create(parent_key).metadata
            root = root_of(parent_meta, default=parent_key)
            session = Session(key=f"subagent:{task_id}", messages=list(result.messages))
            session.metadata.update(build_lineage(
                parent_session_id=parent_key, root_id=root,
                origin_type="subagent", origin_id=task_id,
            ))
            session.metadata["title"] = f"subagent: {label}"
            self._sessions.save(session)
        except Exception:
            logger.exception("Subagent [{}] session persist failed", task_id)

    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
        origin_message_id: str | None = None,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        status_text = "completed successfully" if status == "ok" else "failed"

        announce_content = render_template(
            "agent/subagent_announce.md",
            label=label,
            status_text=status_text,
            task=task,
            result=result,
        )

        # Inject as system message to trigger main agent.
        # Use session_key_override to align with the main agent's effective
        # session key (which accounts for unified sessions) so the result is
        # routed to the correct pending queue (mid-turn injection) instead of
        # being dispatched as a competing independent task.
        override = origin.get("session_key") or f"{origin['channel']}:{origin['chat_id']}"
        metadata: dict[str, Any] = {
            "injected_event": "subagent_result",
            "subagent_task_id": task_id,
        }
        if origin_message_id:
            metadata["origin_message_id"] = origin_message_id
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=announce_content,
            session_key_override=override,
            metadata=metadata,
        )

        await self.bus.publish_inbound(msg)

        # Structured user-facing copy: a synthetic tool_event rides the
        # existing tool_hint pipeline (websocket frame → webui card, TUI
        # bubble, transcript replay). The inbound announce above remains
        # the MODEL's context — the model adds a brief natural summary; the
        # card shows the full result (payload-canonical contract).
        event: dict[str, Any] = {
            "version": 1,
            "phase": "end" if status == "ok" else "error",
            "call_id": f"subagent:{task_id}",
            "name": "subagent_result",
            "arguments": {"label": label, "task": task},
            "result": result,
        }
        if status != "ok":
            event["error"] = result
        with suppress(Exception):
            from durin.bus.events import OutboundMessage

            await self.bus.publish_outbound(OutboundMessage(
                channel=origin["channel"],
                chat_id=origin["chat_id"],
                content="",
                metadata={"_tool_hint": True, "_tool_events": [event]},
            ))
        logger.debug("Subagent [{}] announced result to {}:{}", task_id, origin['channel'], origin['chat_id'])

    @staticmethod
    def _format_partial_progress(result) -> str:
        completed = [e for e in result.tool_events if e["status"] == "ok"]
        failure = next((e for e in reversed(result.tool_events) if e["status"] == "error"), None)
        lines: list[str] = []
        if completed:
            lines.append("Completed steps:")
            for event in completed[-3:]:
                lines.append(f"- {event['name']}: {event['detail']}")
        if failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {failure['name']}: {failure['detail']}")
        if result.error and not failure:
            if lines:
                lines.append("")
            lines.append("Failure:")
            lines.append(f"- {result.error}")
        return "\n".join(lines) or (result.error or "Error: subagent execution failed.")

    def _build_subagent_prompt(self) -> str:
        """Build a focused system prompt for the subagent."""
        from durin.agent.context import ContextBuilder
        from durin.agent.skills import SkillsLoader

        time_ctx = ContextBuilder._build_runtime_context(None, None)
        skills_summary = SkillsLoader(
            self.workspace,
            disabled_skills=self.disabled_skills,
        ).build_skills_summary()
        return render_template(
            "agent/subagent_system.md",
            time_ctx=time_ctx,
            workspace=str(self.workspace),
            skills_summary=skills_summary or "",
        )

    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all subagents for the given session. Returns count cancelled."""
        tasks = [self._running_tasks[tid] for tid in self._session_tasks.get(session_key, [])
                 if tid in self._running_tasks and not self._running_tasks[tid].done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)

    def get_running_count_by_session(self, session_key: str) -> int:
        """Return the number of currently running subagents for a session."""
        tids = self._session_tasks.get(session_key, set())
        return sum(
            1 for tid in tids
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        )

    # ------------------------------------------------------------------
    # Lifecycle inspection — surfaces used by the subagent_* tools.
    # ------------------------------------------------------------------

    def _is_running(self, task_id: str) -> bool:
        t = self._running_tasks.get(task_id)
        return t is not None and not t.done()

    def _remember_finished(self, task_id: str) -> None:
        """Trim ``_task_statuses`` if it grew past the LRU window.

        Removes the oldest entries first (dict insertion order), and
        cleans up their ``_session_tasks`` membership so the per-session
        list stays consistent. The running task's status is never the
        oldest by definition, so the running set is never affected.
        """
        if len(self._task_statuses) <= self._max_status_history:
            return
        excess = len(self._task_statuses) - self._max_status_history
        for old in list(self._task_statuses)[:excess]:
            old_status = self._task_statuses.pop(old, None)
            if old_status and old_status.session_key:
                ids = self._session_tasks.get(old_status.session_key)
                if ids is not None:
                    ids.discard(old)
                    if not ids:
                        self._session_tasks.pop(old_status.session_key, None)

    def list_for_session(self, session_key: str) -> list[SubagentStatus]:
        """All subagent statuses (running + completed) for *session_key*.

        Ordered by ``started_at`` ascending so the natural read is oldest
        → newest. The list reflects the LRU window: very old completed
        subagents have already been trimmed.
        """
        ids = self._session_tasks.get(session_key) or set()
        out = [self._task_statuses[t] for t in ids if t in self._task_statuses]
        return sorted(out, key=lambda s: s.started_at)

    def get_status_for(self, task_id: str, session_key: str) -> SubagentStatus | None:
        """Status for *task_id* if it belongs to *session_key*, else None.

        The session check is the security boundary: a session cannot
        observe a subagent it did not spawn, even if it guesses the id.
        """
        status = self._task_statuses.get(task_id)
        if status is None or status.session_key != session_key:
            return None
        return status

    async def stop_task(self, task_id: str, session_key: str) -> str:
        """Cancel a running subagent. Returns a human-readable result.

        - ``"stopped"`` — the task was running and got cancelled
        - ``"not_running"`` — found but already finished (no-op)
        - ``"unknown"`` — no such task in this session (also covers the
          cross-session case; we never reveal whether the id exists
          elsewhere)
        """
        status = self.get_status_for(task_id, session_key)
        if status is None:
            return "unknown"
        bg_task = self._running_tasks.get(task_id)
        if bg_task is None or bg_task.done():
            return "not_running"
        bg_task.cancel()
        try:
            await bg_task
        except (asyncio.CancelledError, Exception):
            pass
        status.phase = "cancelled"
        status.stop_reason = "cancelled"
        if status.ended_at is None:
            status.ended_at = time.monotonic()
        return "stopped"

    def get_output_for(self, task_id: str, session_key: str) -> dict[str, Any] | None:
        """Lookup the (possibly partial) output for a completed subagent.

        Returns a dict ``{phase, final_content, error, stop_reason}``
        when the task belongs to *session_key*; otherwise ``None``. The
        caller decides how to render — the manager just exposes raw
        fields off the status.
        """
        status = self.get_status_for(task_id, session_key)
        if status is None:
            return None
        return {
            "phase": status.phase,
            "is_running": self._is_running(task_id),
            "final_content": status.final_content,
            "error": status.error,
            "stop_reason": status.stop_reason,
        }

    def monitor_since(
        self,
        task_id: str,
        session_key: str,
        after_event: int = 0,
    ) -> dict[str, Any] | None:
        """Diff snapshot of a subagent's progress since *after_event*.

        Returns ``None`` if the task is unknown (or belongs to another
        session). Otherwise:

        ``{phase, iteration, is_running, events_total, events_since,
          next_cursor, finished, final_content, error, stop_reason}``

        ``events_since`` is the sublist of ``status.tool_events`` from
        index ``after_event`` onward. ``next_cursor`` is the index the
        caller should pass on the next poll to skip what it just saw.
        ``finished`` is True when the task is no longer running, and
        when finished we also include the final output / error / stop
        reason so a single follow-up monitor call can wrap things up
        without a second round-trip to ``subagent_output``.
        """
        status = self.get_status_for(task_id, session_key)
        if status is None:
            return None
        all_events = list(status.tool_events or [])
        cursor = max(0, min(int(after_event or 0), len(all_events)))
        events_since = all_events[cursor:]
        is_running = self._is_running(task_id)
        out: dict[str, Any] = {
            "phase": status.phase,
            "iteration": status.iteration,
            "is_running": is_running,
            "events_total": len(all_events),
            "events_since": events_since,
            "next_cursor": len(all_events),
            "finished": not is_running,
            "label": status.label,
        }
        if not is_running:
            out["final_content"] = status.final_content
            out["error"] = status.error
            out["stop_reason"] = status.stop_reason
        return out
