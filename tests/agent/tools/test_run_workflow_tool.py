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
    # Patch WorkflowEngine.run with a plain synchronous MagicMock so asyncio.to_thread
    # drives it correctly in a worker thread — no AsyncMock, no leaked coroutine.
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
