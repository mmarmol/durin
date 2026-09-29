"""Tests for RunWorkflowTool background-by-default behavior."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.agent.runner import AgentRunResult
from durin.agent.tools.run_workflow import RunWorkflowTool
from durin.config.schema import ToolsConfig, WorkflowConfig
from durin.providers.base import LLMProvider
from durin.session.manager import SessionManager
from durin.workflow.loader import workflows_dir
from durin.workflow.result import WorkflowResult


class _Bus:
    def __init__(self):
        self.injected = []

    async def publish_inbound(self, msg):
        self.injected.append(msg)

    async def publish_outbound(self, msg):
        pass


def _write_noop_workflow(tmp_path):
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / "noop.json").write_text(
        json.dumps({
            "name": "noop",
            "start": "a",
            "nodes": [{"id": "a", "kind": "work", "prompt": "do p", "next": None}],
        }),
        encoding="utf-8",
    )


def _make_tool(tmp_path, bus=None):
    _write_noop_workflow(tmp_path)
    sessions = SessionManager(workspace=tmp_path)
    app_config = SimpleNamespace(
        resolve_default_preset=lambda: object(),
        tools=ToolsConfig(),
        workflow=WorkflowConfig(),
    )
    ctx = SimpleNamespace(
        workspace=str(tmp_path),
        sessions=sessions,
        app_config=app_config,
        bus=bus,
    )
    return RunWorkflowTool.create(ctx)


def _fake_provider():
    p = MagicMock(spec=LLMProvider)
    p.get_default_model.return_value = "test-model"
    return p


@pytest.mark.asyncio
async def test_background_is_the_default(tmp_path):
    bus = _Bus()
    tool = _make_tool(tmp_path, bus=bus)
    # Patch WorkflowEngine.run with a plain synchronous MagicMock so the run's worker
    # thread drives it correctly — no AsyncMock, no leaked coroutine.
    # The patch must remain active through the background task's execution (not just the
    # execute() call), so it wraps both the launch and the sleep.
    canned = WorkflowResult(status="completed", final_output="ok", runs=[], run_id="r1")
    with patch("durin.providers.factory.make_provider", return_value=_fake_provider()), \
         patch("durin.workflow.engine.WorkflowEngine.run", MagicMock(return_value=canned)):
        out = await tool.execute(name="noop", task="hi")
        assert "started in the background" in out
        # Let the background task complete so the result is injected.
        await asyncio.sleep(0.05)
    # Confirm the background path ran and injected its result back into the bus.
    assert bus.injected, "background workflow did not inject its result into the bus"


@pytest.mark.asyncio
async def test_foreground_is_opt_in(tmp_path):
    tool = _make_tool(tmp_path, bus=_Bus())
    with patch("durin.providers.factory.make_provider", return_value=_fake_provider()), \
         patch("durin.agent.runner.AgentRunner.run",
               AsyncMock(return_value=AgentRunResult(
                   final_content="done", messages=[{"role": "assistant", "content": "done"}]
               ))):
        out = await tool.execute(name="noop", task="hi", background=False)
    assert "Workflow run" in out and "completed" in out


def _engine_run_recording_thread(names: list[str]):
    """A WorkflowEngine.run stand-in that records the thread it ran on."""
    import threading

    canned = WorkflowResult(status="completed", final_output="ok", runs=[], run_id="r1")

    def _run(*_args, **_kwargs):
        names.append(threading.current_thread().name)
        return canned

    return _run


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [True, False])
async def test_the_engine_runs_on_a_dedicated_thread(tmp_path, background):
    """A workflow run can take an hour: it gets a thread of its own instead
    of holding one of the event loop's few shared default-executor threads,
    which every short blocking hop in the gateway needs."""
    bus = _Bus()
    tool = _make_tool(tmp_path, bus=bus)
    names: list[str] = []
    with patch("durin.providers.factory.make_provider", return_value=_fake_provider()), \
         patch("durin.workflow.engine.WorkflowEngine.run",
               _engine_run_recording_thread(names)):
        await tool.execute(name="noop", task="hi", background=background)
        # A background run reports back through the bus once it is over.
        for _ in range(200):
            if bus.injected or not background:
                break
            await asyncio.sleep(0.01)
    assert len(names) == 1
    assert names[0].startswith("workflow-run-"), names[0]


@pytest.mark.asyncio
async def test_a_burst_of_background_runs_executes_at_most_the_run_bound_at_once(tmp_path):
    """Background launches return at once, so nothing paces them: runs past
    the bound wait their turn instead of each starting a thread, and every
    one still reports back."""
    import threading

    from durin.workflow.run_threads import MAX_CONCURRENT_RUNS

    bus = _Bus()
    tool = _make_tool(tmp_path, bus=bus)
    gate = threading.Event()
    lock = threading.Lock()
    counts = {"running": 0, "peak": 0}
    canned = WorkflowResult(status="completed", final_output="ok", runs=[], run_id="r1")

    def _gated_run(*_args, **_kwargs):
        with lock:
            counts["running"] += 1
            counts["peak"] = max(counts["peak"], counts["running"])
        try:
            gate.wait(10.0)
            return canned
        finally:
            with lock:
                counts["running"] -= 1

    burst = MAX_CONCURRENT_RUNS + 2
    with patch("durin.providers.factory.make_provider", return_value=_fake_provider()), \
         patch("durin.workflow.engine.WorkflowEngine.run", _gated_run):
        try:
            for _ in range(burst):
                await tool.execute(name="noop", task="hi", background=True)
            for _ in range(500):
                if counts["running"] >= MAX_CONCURRENT_RUNS:
                    break
                await asyncio.sleep(0.01)
            # Give any run that was not held back the time to start too.
            await asyncio.sleep(0.3)
            peak = counts["peak"]
        finally:
            gate.set()
        for _ in range(1000):
            if len(bus.injected) == burst:
                break
            await asyncio.sleep(0.01)

    assert peak == MAX_CONCURRENT_RUNS
    assert len(bus.injected) == burst
