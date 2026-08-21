"""/stop must reach a foreground workflow's engine thread.

Cancelling the turn's asyncio task cannot touch the worker thread running
``WorkflowEngine.run`` — without a bridge the engine keeps burning tokens to
completion with nobody waiting. ``run_workflow`` now signals the cooperative
cancel flag on ``CancelledError`` so the engine stops before its next node,
and drops the flag once the (detached) engine actually stops.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from durin.agent.tools.run_workflow import RunWorkflowTool
from durin.config.schema import ToolsConfig, WorkflowConfig
from durin.providers.base import LLMProvider
from durin.session.manager import SessionManager
from durin.workflow import cancellation
from durin.workflow.engine import WorkflowEngine
from durin.workflow.loader import workflows_dir
from durin.workflow.result import WorkflowResult


def _tool(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    app_config = SimpleNamespace(
        resolve_default_preset=lambda: object(),
        tools=ToolsConfig(),
        workflow=WorkflowConfig(),
    )
    ctx = SimpleNamespace(workspace=str(tmp_path), sessions=sessions, app_config=app_config)
    return RunWorkflowTool.create(ctx)


def _write_workflow(tmp_path, name, data):
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def _any_cancel_requested() -> bool:
    with cancellation._lock:
        return bool(cancellation._cancelled)


def _any_hard_cancel_requested() -> bool:
    with cancellation._lock:
        return any(mode == "hard" for mode in cancellation._cancelled.values())


@pytest.mark.asyncio
async def test_stop_cancels_foreground_engine_cooperatively(tmp_path):
    _write_workflow(tmp_path, "slow", {
        "name": "slow", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "p", "next": None}],
    })
    tool = _tool(tmp_path)

    started = threading.Event()
    engine_stopped = threading.Event()

    def fake_engine_run(self, workflow, task, *, root_session_key=None,
                        input_files=None, output_format=None, resume=None, work_key=None):
        # Stand-in for a long engine walk: poll the cooperative flag the way
        # the real engine's cancel_check does between nodes.
        started.set()
        deadline = time.time() + 5
        while time.time() < deadline and not _any_cancel_requested():
            time.sleep(0.01)
        engine_stopped.set()
        return WorkflowResult(status="cancelled", final_output=None, run_id="r")

    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "test-model"

    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch.object(WorkflowEngine, "run", fake_engine_run):
        turn = asyncio.create_task(
            tool.execute(name="slow", task="t", background=False)
        )
        assert await asyncio.to_thread(started.wait, 2), "engine never started"

        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

        # The cooperative flag reached the engine, which stopped early
        # (well before its own 5s deadline).
        assert await asyncio.to_thread(engine_stopped.wait, 2), (
            "engine thread did not observe the cooperative cancel"
        )

        # Once the detached engine finished, the flag is dropped — the
        # registry does not grow without bound.
        deadline = time.time() + 2
        while time.time() < deadline and _any_cancel_requested():
            await asyncio.sleep(0.01)
        assert not _any_cancel_requested()


@pytest.mark.asyncio
async def test_normal_completion_leaves_no_cancel_flag(tmp_path):
    _write_workflow(tmp_path, "quick", {
        "name": "quick", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "p", "next": None}],
    })
    tool = _tool(tmp_path)

    def fake_engine_run(self, workflow, task, **kwargs):
        return WorkflowResult(status="completed", final_output="42", run_id="r")

    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "test-model"

    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch.object(WorkflowEngine, "run", fake_engine_run):
        out = await tool.execute(name="quick", task="t", background=False)

    assert "42" in out
    assert not _any_cancel_requested()


@pytest.mark.asyncio
async def test_stop_on_a_foreground_run_cancels_hard(tmp_path):
    """Nobody is waiting for a foreground run whose turn was cancelled, so
    letting the in-flight node finish only burns tokens: /stop escalates to the
    hard mode, which interrupts it."""
    _write_workflow(tmp_path, "slow", {
        "name": "slow", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "p", "next": None}],
    })
    tool = _tool(tmp_path)

    started = threading.Event()
    saw_hard = threading.Event()

    def fake_engine_run(self, workflow, task, *, root_session_key=None,
                        input_files=None, output_format=None, resume=None, work_key=None):
        started.set()
        deadline = time.time() + 5
        while time.time() < deadline and not _any_cancel_requested():
            time.sleep(0.01)
        if _any_hard_cancel_requested():
            saw_hard.set()
        return WorkflowResult(status="cancelled", final_output=None, run_id="r")

    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "test-model"

    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch.object(WorkflowEngine, "run", fake_engine_run):
        turn = asyncio.create_task(tool.execute(name="slow", task="t", background=False))
        assert await asyncio.to_thread(started.wait, 2), "engine never started"

        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

        assert await asyncio.to_thread(saw_hard.wait, 2), (
            "the foreground /stop must request the HARD mode, not the graceful one"
        )

        deadline = time.time() + 2
        while time.time() < deadline and _any_cancel_requested():
            await asyncio.sleep(0.01)
        assert not _any_cancel_requested()


@pytest.mark.asyncio
async def test_the_engine_is_wired_with_both_cancel_polls(tmp_path):
    """Regression guard on the production wiring: a run that only gets the
    plain check can never be force-stopped, and the miss is invisible in tests
    that build their own engine."""
    _write_workflow(tmp_path, "quick", {
        "name": "quick", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "p", "next": None}],
    })
    tool = _tool(tmp_path)
    captured: dict = {}

    real_init = WorkflowEngine.__init__

    def spy_init(self, *args, **kwargs):
        captured.update(kwargs)
        real_init(self, *args, **kwargs)

    def fake_engine_run(self, workflow, task, **kwargs):
        return WorkflowResult(status="completed", final_output="ok", run_id="r")

    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "test-model"

    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch.object(WorkflowEngine, "__init__", spy_init), \
         patch.object(WorkflowEngine, "run", fake_engine_run):
        await tool.execute(name="quick", task="t", background=False)

    assert captured.get("cancel_check") is not None
    hard = captured.get("hard_cancel_check")
    assert hard is not None
    assert hard() is False
    cancellation.request_cancel("probe-run", hard=True)
    try:
        # The poll is keyed to this run's own id, not to "anything is cancelled".
        assert hard() is False
    finally:
        cancellation.clear("probe-run")
