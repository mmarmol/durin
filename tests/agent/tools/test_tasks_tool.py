"""The unified `tasks` tool: list / status / stop over sub-agents + workflow runs."""

import json
import time
from pathlib import Path

import pytest

from durin.agent.tools.context import RequestContext
from durin.agent.tools.tasks_tool import TasksTool
from durin.workflow import cancellation

SESSION = "websocket:chatA"


class _SAStatus:
    def __init__(self, task_id, label, phase, iteration=1, usage=None, tool_events=None,
                 error=None, stop_reason=None):
        self.task_id = task_id
        self.label = label
        self.phase = phase
        self.iteration = iteration
        self.session_key = f"subagent:{task_id}"
        self.started_at = time.monotonic()
        self.ended_at = None
        self.usage = usage or {}
        self.tool_events = tool_events or []
        self.error = error
        self.stop_reason = stop_reason


class _FakeManager:
    def __init__(self, statuses, running):
        self._statuses = {s.task_id: s for s in statuses}
        self._running = set(running)
        self.stopped = []

    def list_for_session(self, sk):
        return list(self._statuses.values())

    def get_status_for(self, tid, sk):
        return self._statuses.get(tid)

    def _is_running(self, tid):
        return tid in self._running

    async def stop_task(self, tid, sk):
        if tid not in self._statuses:
            return "unknown"
        self.stopped.append(tid)
        return "stopped" if tid in self._running else "not_running"


def _write_manifest(workspace, name, run_id, *, status, final_output=None, task="the task"):
    d = Path(workspace) / "workflows-runs" / name
    d.mkdir(parents=True, exist_ok=True)
    rec = {
        "schema": 2, "run_id": run_id, "workflow": name, "status": status,
        "root_session_key": SESSION, "started_at": time.time(),
        "finished_at": None if status == "running" else time.time(),
        "ts": time.time(), "task": task,
        "runs": [{"node_id": "n1", "iteration": 1, "status": "ok", "session_key": f"workflow:{run_id}:n1:1"}],
    }
    if final_output is not None:
        rec["final_output"] = final_output
    (d / f"{run_id}.json").write_text(json.dumps(rec), encoding="utf-8")


def _tool(workspace, manager):
    t = TasksTool(workspace=str(workspace), subagent_manager=manager, sessions=None)
    t.set_context(RequestContext(channel="websocket", chat_id="chatA", session_key=SESSION))
    return t


@pytest.mark.asyncio
async def test_list_shows_both_kinds(tmp_path):
    mgr = _FakeManager([_SAStatus("sa01", "research", "awaiting_tools")], running=["sa01"])
    _write_manifest(tmp_path, "qa", "wf01abcd", status="running")
    out = await _tool(tmp_path, mgr).execute(action="list")
    assert "background task(s)" in out
    assert "sa01" in out and "subagent" in out
    assert "wf01abcd" in out and "workflow" in out


@pytest.mark.asyncio
async def test_status_subagent_detail(tmp_path):
    mgr = _FakeManager(
        [_SAStatus("sa01", "research", "awaiting_tools", iteration=3,
                   tool_events=[{"name": "grep", "status": "ok", "detail": "x"}])],
        running=["sa01"],
    )
    out = await _tool(tmp_path, mgr).execute(action="status", id="sa01")
    assert "Sub-agent [sa01]" in out
    assert "phase:" in out and "iteration: 3" in out
    assert "grep" in out


@pytest.mark.asyncio
async def test_status_workflow_detail_includes_final_output(tmp_path):
    _write_manifest(tmp_path, "qa", "wf01abcd", status="completed", final_output="THE ANSWER 42")
    out = await _tool(tmp_path, _FakeManager([], running=[])).execute(action="status", id="wf01abcd")
    assert "Workflow run [wf01abcd]" in out
    assert "status: done" in out
    assert "THE ANSWER 42" in out


@pytest.mark.asyncio
async def test_status_unknown_id(tmp_path):
    out = await _tool(tmp_path, _FakeManager([], running=[])).execute(action="status", id="nope")
    assert "unknown task id" in out


@pytest.mark.asyncio
async def test_stop_subagent_delegates_to_manager(tmp_path):
    mgr = _FakeManager([_SAStatus("sa01", "research", "awaiting_tools")], running=["sa01"])
    out = await _tool(tmp_path, mgr).execute(action="stop", id="sa01")
    assert "cancelled" in out
    assert mgr.stopped == ["sa01"]


@pytest.mark.asyncio
async def test_stop_running_workflow_requests_cancel(tmp_path):
    _write_manifest(tmp_path, "qa", "wf99cancel", status="running")
    try:
        out = await _tool(tmp_path, _FakeManager([], running=[])).execute(action="stop", id="wf99cancel")
        assert "asked to cancel" in out
        assert cancellation.is_cancelled("wf99cancel") is True
    finally:
        cancellation.clear("wf99cancel")


@pytest.mark.asyncio
async def test_stop_finished_workflow_is_noop(tmp_path):
    _write_manifest(tmp_path, "qa", "wfdone001", status="completed", final_output="done")
    out = await _tool(tmp_path, _FakeManager([], running=[])).execute(action="stop", id="wfdone001")
    assert "already" in out
    assert cancellation.is_cancelled("wfdone001") is False


@pytest.mark.asyncio
async def test_unknown_action(tmp_path):
    out = await _tool(tmp_path, _FakeManager([], running=[])).execute(action="frobnicate")
    assert "unknown action" in out


@pytest.mark.asyncio
async def test_status_workflow_shows_work_dir_durations_and_files(tmp_path):
    wd = tmp_path / "wd"
    wd.mkdir()
    (wd / "context.json").write_text("{}")
    _write_manifest(tmp_path, "qa", "wf01abcd", status="running")
    # Enrich the manifest with the fields the engine now records.
    p = tmp_path / "workflows-runs" / "qa" / "wf01abcd.json"
    rec = json.loads(p.read_text())
    rec["work_dir"] = str(wd)
    rec["runs"][0]["duration_s"] = 3.2
    p.write_text(json.dumps(rec), encoding="utf-8")
    out = await _tool(tmp_path, _FakeManager([], running=[])).execute(action="status", id="wf01abcd")
    assert f"work dir: {wd}" in out
    assert "(3.2s)" in out
    assert "context.json" in out and "2 B" in out
