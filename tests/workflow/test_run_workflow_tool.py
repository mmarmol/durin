"""Tests for the run_workflow agent tool (wiring: load -> engine -> run -> summary)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.agent.runner import AgentRunResult
from durin.agent.tools.run_workflow import RunWorkflowTool, _format_result
from durin.config.schema import ToolsConfig, WorkflowConfig
from durin.providers.base import LLMProvider
from durin.session.manager import SessionManager
from durin.workflow.loader import workflows_dir
from durin.workflow.result import NodeRun, WorkflowResult


def _tool(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    app_config = SimpleNamespace(resolve_default_preset=lambda: object(), tools=ToolsConfig(), workflow=WorkflowConfig())
    ctx = SimpleNamespace(workspace=str(tmp_path), sessions=sessions, app_config=app_config)
    return RunWorkflowTool.create(ctx)


def _write_workflow(tmp_path, name, data):
    d = workflows_dir(tmp_path)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def test_tool_metadata():
    sessions = MagicMock()
    ctx = SimpleNamespace(workspace="/tmp", sessions=sessions, app_config=SimpleNamespace(tools=ToolsConfig(), workflow=WorkflowConfig()))
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
async def test_run_writes_a_diagnostic_record(tmp_path):
    # Each run persists a per-run record (the dream self-improvement diagnostic source).
    _write_workflow(tmp_path, "checker", {
        "name": "checker", "start": "gate",
        "nodes": [{"id": "gate", "kind": "decision", "command": "true",
                   "on_pass": None, "on_fail": None}],
    })
    tool = _tool(tmp_path)
    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "test-model"
    with patch("durin.providers.factory.make_provider", return_value=fake_provider):
        await tool.execute(name="checker", task="check it")
    from durin.workflow import run_log
    recs = run_log.read_runs_since(tmp_path, "checker")
    assert len(recs) == 1
    assert recs[0]["status"] == "completed"
    assert any(r["node_id"] == "gate" for r in recs[0]["runs"])
    # the record lives under workflows-runs/, not in the versioned workflows/ dir
    assert (tmp_path / "workflows-runs" / "checker").is_dir()
    assert [p.name for p in workflows_dir(tmp_path).glob("*.json")] == ["checker.json"]


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


@pytest.mark.asyncio
async def test_judgment_workflow_runs_end_to_end(tmp_path):
    from durin.agent.runner import AgentRunResult
    _write_workflow(tmp_path, "reviewed", {
        "name": "reviewed", "start": "make",
        "nodes": [
            {"id": "make", "kind": "work", "next": "review"},
            {"id": "review", "kind": "decision", "criteria": "Is it good?",
             "on_pass": None, "on_fail": "make"},
        ],
    })
    tool = _tool(tmp_path)
    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "test-model"
    # work node returns work; judge returns PASS — both via AgentRunner.run
    results = iter([
        AgentRunResult(final_content="the code", messages=[{"role": "assistant", "content": "the code"}]),
        AgentRunResult(final_content="PASS good", messages=[]),
    ])
    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.agent.runner.AgentRunner.run", AsyncMock(side_effect=lambda *a, **k: next(results))):
        out = await tool.execute(name="reviewed", task="do it")
    assert "completed" in out.lower()
    assert "review" in out


@pytest.mark.asyncio
async def test_subworkflow_runs_end_to_end(tmp_path):
    from durin.agent.runner import AgentRunResult
    _write_workflow(tmp_path, "child", {
        "name": "child", "start": "c",
        "nodes": [{"id": "c", "kind": "work", "next": None}],
    })
    _write_workflow(tmp_path, "parent", {
        "name": "parent", "start": "callchild",
        "nodes": [{"id": "callchild", "kind": "subworkflow", "workflow": "child", "next": None}],
    })
    tool = _tool(tmp_path)
    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "test-model"
    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.agent.runner.AgentRunner.run",
               AsyncMock(return_value=AgentRunResult(final_content="child did it", messages=[]))):
        out = await tool.execute(name="parent", task="go")
    assert "completed" in out.lower()
    assert "callchild" in out


@pytest.mark.asyncio
async def test_run_anchors_node_sessions_to_invoking_session(tmp_path):
    from unittest.mock import AsyncMock
    from durin.agent.runner import AgentRunResult
    from durin.agent.tools.context import RequestContext
    _write_workflow(tmp_path, "w", {"name": "w", "start": "a",
                                    "nodes": [{"id": "a", "kind": "work", "next": None}]})
    sessions = SessionManager(workspace=tmp_path)
    app_config = SimpleNamespace(resolve_default_preset=lambda: object(), tools=ToolsConfig(), workflow=WorkflowConfig())
    ctx = SimpleNamespace(workspace=str(tmp_path), sessions=sessions, app_config=app_config)
    tool = RunWorkflowTool.create(ctx)
    tool.set_context(RequestContext(channel="websocket", chat_id="abc", session_key="websocket:abc"))
    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.agent.runner.AgentRunner.run",
               AsyncMock(return_value=AgentRunResult(final_content="x", messages=[{"role": "assistant", "content": "x"}]))):
        await tool.execute(name="w", task="t")
    kids = sessions.children_of("websocket:abc")
    assert kids and kids[0]["origin_type"] == "workflow_node"


def test_exhausted_run_renders_gracefully():
    result = WorkflowResult(
        status="exhausted",
        run_id="run-abc",
        exhausted_node="check",
        final_output="my best attempt",
        runs=[
            NodeRun(node_id="check", iteration=1, output="has issues", passed=False),
            NodeRun(node_id="check", iteration=2, output="still has a bug on line 4", passed=False),
        ],
    )
    text = _format_result(result)
    assert "did not complete" in text.lower()
    assert "check" in text
    assert "still has a bug on line 4" in text
    assert "my best attempt" in text


def test_completed_run_format_unchanged():
    result = WorkflowResult(
        status="completed",
        run_id="run-xyz",
        exhausted_node=None,
        final_output="the final answer",
        runs=[
            NodeRun(node_id="make", iteration=1, output="draft", session_key="ws:s1"),
            NodeRun(node_id="review", iteration=1, output="pass", passed=True),
        ],
    )
    text = _format_result(result)
    assert "did not complete" not in text.lower()
    # byte-exact regression guard: a completed run must render exactly as before
    assert text == (
        "Workflow run run-xyz: completed\n"
        "  [make#1] -> ws:s1\n"
        "  [review#1] decision: pass\n"
        "\nFinal output:\nthe final answer"
    )


def test_aborted_run_renders_gracefully():
    result = WorkflowResult(
        status="aborted",
        run_id="run-789",
        exhausted_node=None,
        final_output="partial work",
        runs=[
            NodeRun(node_id="work", iteration=1, output="incomplete", session_key=None),
        ],
    )
    text = _format_result(result)
    assert "did not complete" in text.lower()
    assert "partial work" in text
