"""Tests for the subagent lifecycle tools (list, status, stop, output)."""

from __future__ import annotations

import asyncio
import time

import pytest

from durin.agent.subagent import SubagentStatus
from durin.agent.tools.context import RequestContext
from durin.agent.tools.subagent_lifecycle import (
    SubagentListTool,
    SubagentMonitorTool,
    SubagentOutputTool,
    SubagentStatusTool,
    SubagentStopTool,
)


class _FakeManager:
    """Minimal SubagentManager substitute backed by an in-memory dict.

    Exposes only the methods the lifecycle tools call. Keeps the tests
    decoupled from the real LLM-driven runner.
    """

    def __init__(self) -> None:
        self._task_statuses: dict[str, SubagentStatus] = {}
        self._session_tasks: dict[str, set[str]] = {}
        self._running_tasks: dict[str, object] = {}

    def add_status(
        self,
        task_id: str,
        session_key: str,
        *,
        running: bool = False,
        label: str = "label",
        phase: str = "done",
        final_content: str | None = "result text",
        error: str | None = None,
        tool_events: list | None = None,
        iteration: int = 0,
        ended_at: float | None = None,
        stop_reason: str | None = None,
    ) -> SubagentStatus:
        st = SubagentStatus(
            task_id=task_id,
            label=label,
            task_description=label,
            started_at=time.monotonic() - 5.0,
            session_key=session_key,
            phase=phase,
            iteration=iteration,
            final_content=final_content,
            error=error,
            stop_reason=stop_reason,
            tool_events=tool_events or [],
            ended_at=ended_at if not running else None,
        )
        self._task_statuses[task_id] = st
        self._session_tasks.setdefault(session_key, set()).add(task_id)
        if running:
            class _RunningTask:
                def done(self_inner) -> bool:
                    return False
            self._running_tasks[task_id] = _RunningTask()
        return st

    # Methods matching SubagentManager's public surface.

    def _is_running(self, task_id: str) -> bool:
        t = self._running_tasks.get(task_id)
        return t is not None and not t.done()

    def list_for_session(self, session_key: str) -> list[SubagentStatus]:
        ids = self._session_tasks.get(session_key) or set()
        out = [self._task_statuses[t] for t in ids if t in self._task_statuses]
        return sorted(out, key=lambda s: s.started_at)

    def get_status_for(self, task_id: str, session_key: str) -> SubagentStatus | None:
        s = self._task_statuses.get(task_id)
        if s is None or s.session_key != session_key:
            return None
        return s

    async def stop_task(self, task_id: str, session_key: str) -> str:
        s = self.get_status_for(task_id, session_key)
        if s is None:
            return "unknown"
        if not self._is_running(task_id):
            return "not_running"
        self._running_tasks.pop(task_id, None)
        s.phase = "cancelled"
        s.stop_reason = "cancelled"
        s.ended_at = time.monotonic()
        return "stopped"

    def get_output_for(self, task_id: str, session_key: str) -> dict | None:
        s = self.get_status_for(task_id, session_key)
        if s is None:
            return None
        return {
            "phase": s.phase,
            "is_running": self._is_running(task_id),
            "final_content": s.final_content,
            "error": s.error,
            "stop_reason": s.stop_reason,
        }

    def monitor_since(
        self, task_id: str, session_key: str, after_event: int = 0,
    ) -> dict | None:
        s = self.get_status_for(task_id, session_key)
        if s is None:
            return None
        all_events = list(s.tool_events or [])
        cursor = max(0, min(int(after_event or 0), len(all_events)))
        events_since = all_events[cursor:]
        is_running = self._is_running(task_id)
        out = {
            "phase": s.phase,
            "iteration": s.iteration,
            "is_running": is_running,
            "events_total": len(all_events),
            "events_since": events_since,
            "next_cursor": len(all_events),
            "finished": not is_running,
            "label": s.label,
        }
        if not is_running:
            out["final_content"] = s.final_content
            out["error"] = s.error
            out["stop_reason"] = s.stop_reason
        return out


def _ctx(session_key: str = "cli:d") -> RequestContext:
    return RequestContext(channel="cli", chat_id="d", session_key=session_key, metadata={})


def _wire(tool, sess: str = "cli:d") -> None:
    tool.set_context(_ctx(sess))


# ---------------------------------------------------------------------------
# subagent_list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_reports_running_and_finished_counts():
    m = _FakeManager()
    m.add_status("aaa", "cli:d", running=True, label="research")
    m.add_status("bbb", "cli:d", running=False, label="summarize")
    tool = SubagentListTool(manager=m)
    _wire(tool)

    out = await tool.execute()
    assert "2 subagent(s)" in out
    assert "1 running" in out
    assert "1 finished" in out
    assert "[aaa]" in out and "[bbb]" in out


@pytest.mark.asyncio
async def test_list_empty_session_returns_clear_message():
    m = _FakeManager()
    tool = SubagentListTool(manager=m)
    _wire(tool)

    out = await tool.execute()
    assert "No subagents" in out


@pytest.mark.asyncio
async def test_list_excludes_other_sessions():
    """A session must only see its own subagents."""
    m = _FakeManager()
    m.add_status("mine", "cli:d", label="ours")
    m.add_status("theirs", "cli:other", label="not ours")
    tool = SubagentListTool(manager=m)
    _wire(tool)

    out = await tool.execute()
    assert "mine" in out
    assert "theirs" not in out


# ---------------------------------------------------------------------------
# subagent_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_returns_detailed_snapshot():
    m = _FakeManager()
    m.add_status(
        "x1", "cli:d",
        running=True,
        label="long task",
        phase="awaiting_tools",
        iteration=3,
        tool_events=[
            {"name": "read_file", "status": "ok", "detail": "file content"},
            {"name": "grep", "status": "ok", "detail": "matches"},
        ],
    )
    tool = SubagentStatusTool(manager=m)
    _wire(tool)

    out = await tool.execute(task_id="x1")
    assert "x1" in out and "long task" in out
    assert "running" in out
    assert "iteration: 3" in out
    assert "read_file" in out and "grep" in out


@pytest.mark.asyncio
async def test_status_unknown_task_returns_error():
    m = _FakeManager()
    tool = SubagentStatusTool(manager=m)
    _wire(tool)

    out = await tool.execute(task_id="ghost")
    assert "Error" in out
    assert "unknown task_id" in out.lower() or "ghost" in out


@pytest.mark.asyncio
async def test_status_cross_session_returns_error():
    m = _FakeManager()
    m.add_status("x1", "cli:other", label="not ours")
    tool = SubagentStatusTool(manager=m)
    _wire(tool, sess="cli:d")

    out = await tool.execute(task_id="x1")
    assert "Error" in out


# ---------------------------------------------------------------------------
# subagent_stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_cancels_running_subagent():
    m = _FakeManager()
    m.add_status("x1", "cli:d", running=True)
    tool = SubagentStopTool(manager=m)
    _wire(tool)

    out = await tool.execute(task_id="x1")
    assert "cancelled" in out.lower()
    assert not m._is_running("x1")


@pytest.mark.asyncio
async def test_stop_not_running_returns_clear_message():
    m = _FakeManager()
    m.add_status("x1", "cli:d", running=False)
    tool = SubagentStopTool(manager=m)
    _wire(tool)

    out = await tool.execute(task_id="x1")
    assert "already finished" in out.lower()


@pytest.mark.asyncio
async def test_stop_unknown_task_returns_error():
    m = _FakeManager()
    tool = SubagentStopTool(manager=m)
    _wire(tool)

    out = await tool.execute(task_id="ghost")
    assert "Error" in out


@pytest.mark.asyncio
async def test_stop_cross_session_returns_error():
    m = _FakeManager()
    m.add_status("x1", "cli:other", running=True)
    tool = SubagentStopTool(manager=m)
    _wire(tool, sess="cli:d")

    out = await tool.execute(task_id="x1")
    assert "Error" in out
    # And the task is still running in the other session — we did NOT
    # cancel it from across sessions.
    assert m._is_running("x1")


# ---------------------------------------------------------------------------
# subagent_output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_output_returns_final_content_for_completed():
    m = _FakeManager()
    m.add_status("done1", "cli:d", running=False, final_content="The answer is 42.")
    tool = SubagentOutputTool(manager=m)
    _wire(tool)

    out = await tool.execute(task_id="done1")
    assert "The answer is 42" in out


@pytest.mark.asyncio
async def test_output_says_running_when_still_in_progress():
    m = _FakeManager()
    m.add_status("running1", "cli:d", running=True, final_content=None, phase="awaiting_tools")
    tool = SubagentOutputTool(manager=m)
    _wire(tool)

    out = await tool.execute(task_id="running1")
    assert "still running" in out.lower()
    assert "awaiting_tools" in out


@pytest.mark.asyncio
async def test_output_unknown_task_returns_error():
    m = _FakeManager()
    tool = SubagentOutputTool(manager=m)
    _wire(tool)

    out = await tool.execute(task_id="ghost")
    assert "Error" in out


@pytest.mark.asyncio
async def test_output_truncates_very_long_results():
    m = _FakeManager()
    big = "x" * 8000
    m.add_status("big", "cli:d", running=False, final_content=big)
    tool = SubagentOutputTool(manager=m)
    _wire(tool)

    out = await tool.execute(task_id="big")
    assert "truncated" in out.lower()


# ---------------------------------------------------------------------------
# Plumbing / mode integration
# ---------------------------------------------------------------------------


def test_tools_are_in_plan_mode_allowed_set():
    """Lifecycle tools are read-only with respect to the workspace and
    must remain callable during plan mode."""
    from durin.agent.agent_mode import PLAN_MODE

    for name in (
        "subagent_list",
        "subagent_status",
        "subagent_stop",
        "subagent_output",
        "subagent_monitor",
    ):
        assert PLAN_MODE.is_tool_allowed(name), f"{name} should be allowed in plan"


def test_tools_discovered_by_loader():
    from durin.agent.tools.loader import ToolLoader

    names = {c.__name__ for c in ToolLoader().discover()}
    assert "SubagentListTool" in names
    assert "SubagentStatusTool" in names
    assert "SubagentStopTool" in names
    assert "SubagentOutputTool" in names
    assert "SubagentMonitorTool" in names


# ---------------------------------------------------------------------------
# subagent_monitor — cursor-based incremental diff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_monitor_returns_all_events_when_cursor_zero():
    m = _FakeManager()
    m.add_status(
        "m1", "cli:d",
        running=True,
        tool_events=[
            {"name": "read_file", "status": "ok", "detail": "a"},
            {"name": "grep", "status": "ok", "detail": "b"},
        ],
    )
    tool = SubagentMonitorTool(manager=m)
    _wire(tool)

    out = await tool.execute(task_id="m1", after_event=0)
    assert "m1" in out
    assert "events_total: 2" in out
    assert "next_cursor:  2" in out
    assert "read_file" in out and "grep" in out


@pytest.mark.asyncio
async def test_monitor_skips_events_below_cursor():
    """The point of the cursor: don't re-deliver what was already seen."""
    m = _FakeManager()
    m.add_status(
        "m1", "cli:d",
        running=True,
        tool_events=[
            {"name": "first", "status": "ok", "detail": "1"},
            {"name": "second", "status": "ok", "detail": "2"},
            {"name": "third", "status": "ok", "detail": "3"},
        ],
    )
    tool = SubagentMonitorTool(manager=m)
    _wire(tool)

    out = await tool.execute(task_id="m1", after_event=2)
    assert "third" in out
    assert "first" not in out
    assert "second" not in out
    assert "next_cursor:  3" in out


@pytest.mark.asyncio
async def test_monitor_clamps_out_of_range_cursor():
    """Cursor past the end yields zero new events, not an error."""
    m = _FakeManager()
    m.add_status(
        "m1", "cli:d",
        running=True,
        tool_events=[{"name": "x", "status": "ok", "detail": "d"}],
    )
    tool = SubagentMonitorTool(manager=m)
    _wire(tool)

    out = await tool.execute(task_id="m1", after_event=999)
    assert "no new events" in out.lower()


@pytest.mark.asyncio
async def test_monitor_finished_subagent_includes_final_output():
    m = _FakeManager()
    m.add_status(
        "done1", "cli:d",
        running=False,
        final_content="result text",
        stop_reason="completed",
        tool_events=[{"name": "ran", "status": "ok", "detail": "ok"}],
    )
    tool = SubagentMonitorTool(manager=m)
    _wire(tool)

    out = await tool.execute(task_id="done1", after_event=0)
    assert "finished" in out
    assert "stop_reason=completed" in out
    assert "result text" in out


@pytest.mark.asyncio
async def test_monitor_unknown_task_returns_error():
    m = _FakeManager()
    tool = SubagentMonitorTool(manager=m)
    _wire(tool)

    out = await tool.execute(task_id="ghost", after_event=0)
    assert "Error" in out


@pytest.mark.asyncio
async def test_monitor_cross_session_returns_error():
    m = _FakeManager()
    m.add_status("m1", "cli:other", running=True)
    tool = SubagentMonitorTool(manager=m)
    _wire(tool, sess="cli:d")

    out = await tool.execute(task_id="m1", after_event=0)
    assert "Error" in out


# ---------------------------------------------------------------------------
# Real SubagentManager: status retention after completion + LRU trim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_manager_retains_status_after_completion(tmp_path):
    """When a subagent task finishes, the status must remain in the
    manager (with final_content set) so subagent_output can still serve
    the result some turns later."""
    from unittest.mock import MagicMock

    from durin.agent.subagent import SubagentManager
    from durin.bus.queue import MessageBus

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=1000,
        model="test-model",
    )

    # Simulate a finished task by registering a status directly.
    st = SubagentStatus(
        task_id="t1",
        label="lbl",
        task_description="desc",
        started_at=time.monotonic(),
        session_key="cli:s1",
        phase="done",
        final_content="result-text",
        ended_at=time.monotonic(),
    )
    mgr._task_statuses["t1"] = st
    mgr._session_tasks.setdefault("cli:s1", set()).add("t1")

    # After a (no-op) "cleanup" pass, the status survives.
    mgr._remember_finished("t1")
    assert "t1" in mgr._task_statuses
    assert mgr.get_output_for("t1", "cli:s1")["final_content"] == "result-text"


@pytest.mark.asyncio
async def test_real_manager_lru_trims_oldest_when_over_cap(tmp_path):
    from unittest.mock import MagicMock

    from durin.agent.subagent import SubagentManager
    from durin.bus.queue import MessageBus

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"

    mgr = SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        max_tool_result_chars=1000,
        model="test-model",
    )
    mgr._max_status_history = 3  # tighten for the test

    for i in range(5):
        tid = f"t{i}"
        mgr._task_statuses[tid] = SubagentStatus(
            task_id=tid,
            label=tid,
            task_description=tid,
            started_at=time.monotonic() + i,
            session_key="cli:s1",
            phase="done",
        )
        mgr._session_tasks.setdefault("cli:s1", set()).add(tid)
        mgr._remember_finished(tid)

    # Only the newest 3 should remain.
    assert set(mgr._task_statuses) == {"t2", "t3", "t4"}
    # And the session index is consistent with the surviving statuses.
    assert mgr._session_tasks["cli:s1"] == {"t2", "t3", "t4"}
