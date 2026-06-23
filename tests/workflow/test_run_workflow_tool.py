"""Tests for the run_workflow agent tool (wiring: load -> engine -> run -> summary)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.agent.runner import AgentRunResult
from durin.agent.tools.run_workflow import RunWorkflowTool
from durin.providers.base import LLMProvider
from durin.session.manager import SessionManager
from durin.workflow.loader import workflows_dir


def _tool(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    app_config = SimpleNamespace(resolve_default_preset=lambda: object())
    ctx = SimpleNamespace(workspace=str(tmp_path), sessions=sessions, app_config=app_config)
    return RunWorkflowTool.create(ctx)


def _write_workflow(tmp_path, name, data):
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def test_tool_metadata():
    sessions = MagicMock()
    ctx = SimpleNamespace(workspace="/tmp", sessions=sessions, app_config=object())
    tool = RunWorkflowTool.create(ctx)
    assert tool.name == "run_workflow"
    assert "name" in tool.parameters["properties"]
    assert "task" in tool.parameters["properties"]
    assert "core" in RunWorkflowTool._scopes


@pytest.mark.asyncio
async def test_missing_workflow_returns_error(tmp_path):
    tool = _tool(tmp_path)
    out = await tool.execute(name="ghost", task="t")
    assert "ghost" in out


@pytest.mark.asyncio
async def test_runs_command_only_workflow_end_to_end(tmp_path):
    # A decision-only workflow needs no LLM: it runs a command and routes to the end.
    _write_workflow(tmp_path, "checker", {
        "name": "checker", "start": "gate",
        "nodes": [{"id": "gate", "kind": "decision", "command": "true",
                   "on_pass": None, "on_fail": None}],
    })
    tool = _tool(tmp_path)
    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "test-model"
    with patch("durin.providers.factory.make_provider", return_value=fake_provider):
        out = await tool.execute(name="checker", task="check it")
    assert "completed" in out.lower()
    assert "gate" in out


@pytest.mark.asyncio
async def test_work_node_runs_through_to_thread_boundary(tmp_path):
    # A work node forces the node runner's inner asyncio.run to execute; it must
    # run inside the asyncio.to_thread worker (no active loop there) to be valid.
    _write_workflow(tmp_path, "doer", {
        "name": "doer", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "p", "next": None}],
    })
    tool = _tool(tmp_path)
    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "test-model"
    fake_result = AgentRunResult(
        final_content="did the work",
        messages=[{"role": "assistant", "content": "did the work"}],
    )
    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.agent.runner.AgentRunner.run", AsyncMock(return_value=fake_result)):
        out = await tool.execute(name="doer", task="do it")
    assert "completed" in out.lower()
    assert "did the work" in out
