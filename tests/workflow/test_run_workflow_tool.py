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
from durin.workflow import run_log
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
    assert "input_files" in tool.parameters["properties"]
    assert "core" in RunWorkflowTool._scopes


@pytest.mark.asyncio
async def test_missing_workflow_returns_error(tmp_path):
    tool = _tool(tmp_path)
    out = await tool.execute(name="ghost", task="t")
    assert "ghost" in out



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
        out = await tool.execute(name="doer", task="do it", background=False)
    assert "completed" in out.lower()
    assert "did the work" in out


@pytest.mark.asyncio
async def test_judgment_workflow_runs_end_to_end(tmp_path):
    from durin.agent.runner import AgentRunResult
    _write_workflow(tmp_path, "reviewed", {
        "name": "reviewed", "start": "make",
        "nodes": [
            {"id": "make", "kind": "work", "next": "review"},
            {"id": "review", "kind": "work", "prompt": "Is it good?",
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
        out = await tool.execute(name="reviewed", task="do it", background=False)
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
        out = await tool.execute(name="parent", task="go", background=False)
    assert "completed" in out.lower()
    assert "callchild" in out


@pytest.mark.asyncio
async def test_script_node_runs_end_to_end(tmp_path):
    # A script-only workflow needs no provider/agent turn at all — proves the
    # tool wires a real ScriptNodeRunner through to the engine.
    _write_workflow(tmp_path, "scripted", {
        "name": "scripted", "start": "s",
        "nodes": [{"id": "s", "kind": "script", "command": "echo tool-ok", "next": None}],
    })
    tool = _tool(tmp_path)
    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "test-model"
    with patch("durin.providers.factory.make_provider", return_value=fake_provider):
        out = await tool.execute(name="scripted", task="go", background=False)
    assert "completed" in out.lower()
    assert "tool-ok" in out
    assert "(no session)" in out


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
        await tool.execute(name="w", task="t", background=False)
    kids = sessions.children_of("websocket:abc")
    assert kids and kids[0]["origin_type"] == "workflow_node"


@pytest.mark.asyncio
async def test_input_files_forwarded_to_engine(tmp_path):
    # The tool must hand input_files through to engine.run so the engine seeds them
    # into the run's shared working folder.
    _write_workflow(tmp_path, "doer", {
        "name": "doer", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "p", "next": None}],
    })
    tool = _tool(tmp_path)
    captured = {}

    def fake_run(self, workflow, task, *, root_session_key=None, input_files=None, output_format=None, resume=None):
        captured["input_files"] = input_files
        return WorkflowResult(status="completed", run_id="r", final_output="ok", runs=[])

    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.workflow.engine.WorkflowEngine.run", fake_run):
        await tool.execute(name="doer", task="t", input_files=["/abs/a.txt", "/abs/b.txt"], background=False)
    assert captured["input_files"] == ["/abs/a.txt", "/abs/b.txt"]


@pytest.mark.asyncio
async def test_resume_rejects_a_run_that_did_not_end_needs_input(tmp_path):
    _write_workflow(tmp_path, "doer", {
        "name": "doer", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "p", "next": None}],
    })
    completed = WorkflowResult(status="completed", run_id="r1", final_output="done", runs=[])
    run_log.finalize_run(
        tmp_path, "doer", completed,
        root_session_key=None, started_at=1.0, finished_at=2.0, task="original task",
    )
    tool = _tool(tmp_path)
    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.workflow.engine.WorkflowEngine.run") as fake_run:
        out = await tool.execute(name="doer", task="prod env", resume_run_id="r1", background=False)
    assert "cannot be resumed" in out
    fake_run.assert_not_called()


@pytest.mark.asyncio
async def test_resume_builds_resume_state_from_the_manifest(tmp_path):
    _write_workflow(tmp_path, "doer", {
        "name": "doer", "start": "plan",
        "nodes": [
            {"id": "plan", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "work", "next": None},
        ],
    })
    needs_input = WorkflowResult(
        status="needs_input", run_id="r1", final_output="what env?", needs_input_node="gate",
        runs=[
            NodeRun(node_id="plan", iteration=1, output="planned"),
            NodeRun(node_id="gate", iteration=1, output="asking"),
        ],
    )
    run_log.finalize_run(
        tmp_path, "doer", needs_input,
        root_session_key=None, started_at=1.0, finished_at=2.0, task="original task",
    )
    tool = _tool(tmp_path)
    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "m"
    captured = {}

    def fake_run(self, workflow, task, *, root_session_key=None, input_files=None,
                 output_format=None, resume=None):
        captured["task"] = task
        captured["resume"] = resume
        return WorkflowResult(status="completed", run_id=resume.run_id, final_output="ok", runs=[])

    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.workflow.engine.WorkflowEngine.run", fake_run):
        await tool.execute(name="doer", task="prod env", resume_run_id="r1", background=False)

    assert captured["resume"].start_at == "gate"
    assert captured["resume"].visits == {"plan": 1, "gate": 1}
    assert "prod env" in captured["resume"].upstream
    assert "what env?" in captured["resume"].upstream
    assert captured["task"] == "original task"


def test_completed_run_surfaces_output_dir_when_files_declared():
    result = WorkflowResult(
        status="completed", run_id="r", exhausted_node=None,
        final_output="ans", output_dir="/ws/.workflow/r/work",
        runs=[NodeRun(node_id="a", iteration=1, output="x", session_key="ws:s1")],
    )
    # Declared file output -> surface the working folder.
    assert "The workflow's output files are in: /ws/.workflow/r/work" in _format_result(result, output_files=True)
    # Pure-text workflow (default) -> no working-folder noise even though output_dir is set.
    assert "output files are in" not in _format_result(result)


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
            NodeRun(node_id="review", iteration=1, output="pass", passed=True, session_key="ws:s2"),
        ],
    )
    text = _format_result(result)
    assert "did not complete" not in text.lower()
    # byte-exact regression guard: a routing node now also surfaces its session key
    assert text == (
        "Workflow run run-xyz: completed\n"
        "  [make#1] -> ws:s1\n"
        "  [review#1] decision: pass -> ws:s2\n"
        "\nFinal output:\nthe final answer"
    )


def test_routing_node_without_session_renders_decision_only():
    result = WorkflowResult(
        status="completed",
        run_id="run-cmd",
        exhausted_node=None,
        final_output="ok",
        runs=[
            NodeRun(node_id="gate", iteration=1, output="", passed=True, session_key=None),
        ],
    )
    text = _format_result(result)
    assert "  [gate#1] decision: pass\n" in text + "\n"


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


def test_completed_run_lists_output_files_with_overflow():
    result = WorkflowResult(
        status="completed", final_output="done", runs=[], run_id="r1",
        output_dir="/tmp/wf/work",
        output_files=[f"f{i:02}.txt" for i in range(25)],
    )
    out = _format_result(result, output_files=True)
    assert "f00.txt" in out and "f19.txt" in out     # first 20 listed
    assert "f20.txt" not in out                       # 21st not listed
    assert "and 5 more" in out                        # overflow line
    assert "Copy out any deliverable" in out          # retention warning


@pytest.mark.asyncio
async def test_tool_passes_keep_runs_to_engine(tmp_path):
    _write_workflow(tmp_path, "simple", {
        "name": "simple", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "p", "next": None}],
    })
    sessions = SessionManager(workspace=tmp_path)
    app_config = SimpleNamespace(
        resolve_default_preset=lambda: object(),
        tools=ToolsConfig(),
        workflow=WorkflowConfig(keep_runs=7)
    )
    ctx = SimpleNamespace(workspace=str(tmp_path), sessions=sessions, app_config=app_config)
    tool = RunWorkflowTool.create(ctx)

    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "test-model"
    fake_result = AgentRunResult(
        final_content="done",
        messages=[{"role": "assistant", "content": "done"}],
    )

    captured_kwargs = {}

    def fake_engine_init(self, **kwargs):
        captured_kwargs.update(kwargs)
        self.run = MagicMock(return_value=WorkflowResult(
            status="completed", run_id="r1", final_output="ok", runs=[]
        ))

    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.agent.runner.AgentRunner.run", AsyncMock(return_value=fake_result)), \
         patch("durin.workflow.engine.WorkflowEngine.__init__", fake_engine_init):
        await tool.execute(name="simple", task="do it", background=False)

    assert captured_kwargs.get("prune_keep") == 7


# ---------------------------------------------------------------------------
# The background waiting contract: the launch reply and the tool description
# must teach push-delivery (end your turn), never sleep+status polling.
# ---------------------------------------------------------------------------

def test_background_launch_message_states_push_contract():
    from durin.agent.tools.run_workflow import _background_launch_message
    msg = _background_launch_message("w", "r1")
    assert "end your turn" in msg.lower()
    assert "do not poll" in msg.lower().replace("not poll", "not poll")
    assert "r1" in msg


def test_run_workflow_description_mentions_cases_and_skill():
    tool = RunWorkflowTool(workspace="/tmp/x", sessions=None, app_config=None)
    d = tool.description
    assert "cases" in d
    assert "workflows` skill" in d or "workflows skill" in d
