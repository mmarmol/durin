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
from durin.workflow.resume_claim import ResumeClaim
from tests.workflow.telemetry_capture import assert_clean, payloads, recorded
from tests.workflow.telemetry_capture import telemetry_dir_fixture as _telemetry_dir  # noqa: F401


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
    # run inside the engine's worker thread (no active loop there) to be valid.
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

    def fake_run(self, workflow, task, *, root_session_key=None, input_files=None, output_format=None, resume=None, work_key=None, launch=None):
        captured["input_files"] = input_files
        return WorkflowResult(status="completed", run_id="r", final_output="ok", runs=[])

    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.workflow.engine.WorkflowEngine.run", fake_run):
        await tool.execute(name="doer", task="t", input_files=["/abs/a.txt", "/abs/b.txt"], background=False)
    assert captured["input_files"] == ["/abs/a.txt", "/abs/b.txt"]


@pytest.mark.asyncio
async def test_work_key_forwarded_to_engine_foreground(tmp_path):
    _write_workflow(tmp_path, "doer", {
        "name": "doer", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "p", "next": None}],
    })
    tool = _tool(tmp_path)
    captured = {}

    def fake_run(self, workflow, task, *, root_session_key=None, input_files=None, output_format=None, resume=None, work_key=None, launch=None):
        captured["work_key"] = work_key
        return WorkflowResult(status="completed", run_id="r", final_output="ok", runs=[])

    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.workflow.engine.WorkflowEngine.run", fake_run):
        await tool.execute(name="doer", task="t", work_key="ticket-1", background=False)
    assert captured["work_key"] == "ticket-1"


@pytest.mark.asyncio
async def test_no_work_key_forwards_none(tmp_path):
    _write_workflow(tmp_path, "doer", {
        "name": "doer", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "p", "next": None}],
    })
    tool = _tool(tmp_path)
    captured = {}

    def fake_run(self, workflow, task, *, root_session_key=None, input_files=None, output_format=None, resume=None, work_key=None, launch=None):
        captured["work_key"] = work_key
        return WorkflowResult(status="completed", run_id="r", final_output="ok", runs=[])

    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.workflow.engine.WorkflowEngine.run", fake_run):
        await tool.execute(name="doer", task="t", background=False)
    assert captured["work_key"] is None


def test_work_key_in_tool_parameters_schema(tmp_path):
    tool = _tool(tmp_path)
    assert "work_key" in tool.parameters["properties"]


@pytest.mark.asyncio
async def test_unsafe_work_key_returns_an_error_without_running(tmp_path):
    """An unsafe work_key must return a clear error string without ever
    dispatching the engine or touching the version store — the tool's
    equivalent of the HTTP launch route's 422, since a tool call has no
    status code to reject with."""
    _write_workflow(tmp_path, "doer", {
        "name": "doer", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "p", "next": None}],
    })
    tool = _tool(tmp_path)
    with patch("durin.providers.factory.make_provider") as fake_make_provider, \
         patch("durin.workflow.engine.WorkflowEngine.run") as fake_run:
        out = await tool.execute(name="doer", task="t", work_key="../evil", background=False)
    assert "work_key" in out.lower()
    assert out.lower().startswith("error")
    fake_make_provider.assert_not_called()
    fake_run.assert_not_called()


@pytest.mark.asyncio
async def test_resume_and_work_key_together_is_a_clear_error(tmp_path):
    """A resumed run already keeps the work_key it was started with, so
    passing a (possibly different) work_key alongside resume_run_id would
    silently relocate it -- the tool rejects the combination outright,
    before even looking up the resume_run_id's manifest, let alone running
    the engine."""
    _write_workflow(tmp_path, "doer", {
        "name": "doer", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "p", "next": None}],
    })
    tool = _tool(tmp_path)
    with patch("durin.providers.factory.make_provider") as fake_make_provider, \
         patch("durin.workflow.engine.WorkflowEngine.run") as fake_run:
        # resume_run_id="r1" has no recorded manifest -- if the conflict check
        # ran after the resume lookup, this would surface a "no run 'r1'
        # recorded" error instead, which the exact-text assertion below rules out.
        out = await tool.execute(
            name="doer", task="t", resume_run_id="r1", work_key="ticket-1", background=False,
        )
    assert out == (
        "Error: pass either resume_run_id or work_key, not both — a resumed "
        "run keeps the work_key it was started with."
    )
    fake_make_provider.assert_not_called()
    fake_run.assert_not_called()


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
async def test_resume_refuses_a_record_whose_failed_node_is_a_branch(tmp_path):
    # Older versions named the branch itself when a choose/union branch failed, and
    # kept no input for it. Resuming there would run that one branch as if it were a
    # step of the walk and call the run completed.
    _write_workflow(tmp_path, "doer", {
        "name": "doer", "start": "fan",
        "nodes": [
            {"id": "fan", "kind": "parallel", "branches": ["a", "b"], "reconcile": "union", "next": None},
            {"id": "a", "kind": "work", "tools": "default"},
            {"id": "b", "kind": "work", "tools": "default"},
        ],
    })
    old = WorkflowResult(status="aborted", run_id="r1", final_output="boom", failed_node="b",
                         runs=[NodeRun(node_id="a", iteration=1, output="x", branch_id="a")])
    run_log.finalize_run(
        tmp_path, "doer", old,
        root_session_key=None, started_at=1.0, finished_at=2.0, task="original task",
    )
    record = tmp_path / "workflows-runs" / "doer" / "r1.json"
    before = record.read_bytes()
    tool = _tool(tmp_path)
    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.workflow.engine.WorkflowEngine.run") as fake_run:
        out = await tool.execute(name="doer", task="", resume_run_id="r1", background=False)
    assert "cannot be resumed" in out and "new run" in out
    fake_run.assert_not_called()
    assert record.read_bytes() == before


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
                 output_format=None, resume=None, work_key=None, launch=None):
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
    # byte-exact guard on the summary's shape: status and counts, then the
    # result, then the node list (a routing node surfaces its session key)
    assert text == (
        "Workflow run run-xyz: completed\n"
        "Node runs: 2 succeeded, 0 failed.\n"
        "\nFinal output:\nthe final answer\n"
        "\nNodes:\n"
        "  [make#1] -> ws:s1\n"
        "  [review#1] decision: pass -> ws:s2"
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


def test_format_result_warns_on_missing_declared_artifacts():
    res = WorkflowResult(
        status="completed", final_output="done", run_id="r1",
        runs=[NodeRun(node_id="s", iteration=1, output="done")],
        output_dir="/tmp/wd", output_files=["context.json"],
        missing_artifacts=["evidence.json"],
    )
    out = _format_result(res, output_files=True)
    assert "evidence.json" in out
    assert "declared" in out.lower() and "not produced" in out.lower()


@pytest.mark.asyncio
async def test_run_uses_the_default_saved_after_the_tool_was_built(tmp_path):
    """Switching the default model (settings screen -> save_config) must reach the
    NEXT workflow run, not only the next gateway restart.

    The tool is constructed once at boot with the config snapshot of that moment;
    a run that resolves its provider from that snapshot keeps calling the model
    the operator already moved away from.
    """
    _write_workflow(tmp_path, "w", {
        "name": "w", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "p", "next": None}],
    })
    boot = SimpleNamespace(resolve_default_preset=lambda: "old-preset",
                           tools=ToolsConfig(), workflow=WorkflowConfig())
    saved = SimpleNamespace(resolve_default_preset=lambda: "new-preset",
                            tools=ToolsConfig(), workflow=WorkflowConfig())
    sessions = SessionManager(workspace=tmp_path)
    ctx = SimpleNamespace(workspace=str(tmp_path), sessions=sessions, app_config=boot,
                          provider_snapshot_loader=lambda *a, **k: None)
    tool = RunWorkflowTool.create(ctx)

    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "test-model"
    seen: list = []

    def _capture(config, *, preset=None, **kw):
        seen.append(preset)
        return fake_provider

    with patch("durin.config.loader.load_config", return_value=saved), \
         patch("durin.config.loader.resolve_config_env_vars", side_effect=lambda c: c), \
         patch("durin.providers.factory.make_provider", side_effect=_capture), \
         patch("durin.agent.runner.AgentRunner.run",
               AsyncMock(return_value=AgentRunResult(final_content="ok", messages=[]))):
        await tool.execute(name="w", task="t", background=False)

    assert seen == ["new-preset"]


@pytest.mark.asyncio
async def test_run_falls_back_to_the_injected_config_without_a_live_context(tmp_path):
    """Embedded/SDK surfaces build the tool with an explicit config and no live
    context — those keep using exactly the config they were handed."""
    _write_workflow(tmp_path, "w", {
        "name": "w", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "p", "next": None}],
    })
    injected = SimpleNamespace(resolve_default_preset=lambda: "injected-preset",
                               tools=ToolsConfig(), workflow=WorkflowConfig())
    tool = RunWorkflowTool.create(SimpleNamespace(
        workspace=str(tmp_path), sessions=SessionManager(workspace=tmp_path), app_config=injected))

    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "test-model"
    seen: list = []

    def _capture(config, *, preset=None, **kw):
        seen.append(preset)
        return fake_provider

    with patch("durin.providers.factory.make_provider", side_effect=_capture), \
         patch("durin.agent.runner.AgentRunner.run",
               AsyncMock(return_value=AgentRunResult(final_content="ok", messages=[]))):
        await tool.execute(name="w", task="t", background=False)

    assert seen == ["injected-preset"]


# ---------------------------------------------------------------------------
# Approval pauses are a person's decision; questions depend on who can answer
# ---------------------------------------------------------------------------


def _fake_node_llm(output: str):
    from durin.providers.base import GenerationSettings

    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    # Real attributes on the spec: unstubbed they would be MagicMocks riding
    # into the run manifest, which then fails to serialize.
    provider.provider_key = "test-provider"
    provider.generation = GenerationSettings()
    result = AgentRunResult(final_content=output, messages=[{"role": "assistant", "content": output}])
    return provider, AsyncMock(return_value=result)


@pytest.mark.asyncio
async def test_resume_of_an_approval_pause_is_refused(tmp_path):
    """Approving, revising or rejecting is a person's decision: every resume
    of an approval pause from this tool is refused and the run stays paused."""
    import re

    _write_workflow(tmp_path, "gated", {
        "name": "gated", "start": "draft",
        "nodes": [{"id": "draft", "kind": "work", "approval": True, "next": None}],
    })
    tool = _tool(tmp_path)
    provider, node_run = _fake_node_llm("the drafted email")
    with patch("durin.providers.factory.make_provider", return_value=provider), \
         patch("durin.agent.runner.AgentRunner.run", node_run):
        first = await tool.execute(name="gated", task="draft it", background=False)
        run_id = re.search(r"Workflow run (\S+): needs_input", first).group(1)
        assert "APPROVAL" in first and "the drafted email" in first
        runs_at_pause = node_run.await_count
        for reply in ("approve", "sí", "reject", "tighten the subject line"):
            out = await tool.execute(name="gated", task=reply, resume_run_id=run_id,
                                     background=False)
            assert out.startswith("Refused"), out

    assert node_run.await_count == runs_at_pause
    manifest = run_log.read_manifest(tmp_path, "gated", run_id)
    assert manifest["status"] == "needs_input"
    assert manifest["ask_kind"] == "approval"


def test_question_pause_summary_sends_the_agent_to_the_user_only_when_one_can_answer():
    paused = WorkflowResult(status="needs_input", ask_kind="question", run_id="r1",
                            final_output="Which account?", needs_input_node="ask")
    with_person = _format_result(paused, human_reachable=True)
    without = _format_result(paused, human_reachable=False)
    assert "ask_user_question" in with_person
    assert "ask_user_question" not in without
    assert "No person can be asked" in without
    assert "resume_run_id='r1'" in without


@pytest.mark.asyncio
async def test_a_question_pause_in_a_cron_session_is_not_sent_to_the_user(tmp_path):
    from durin.agent.tools.context import RequestContext

    _write_workflow(tmp_path, "asker", {
        "name": "asker", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "p", "next": None}],
    })
    tool = _tool(tmp_path)
    tool.set_context(RequestContext(channel="cli", chat_id="c", session_key="cron:nightly"))
    paused = WorkflowResult(status="needs_input", ask_kind="question", run_id="r9",
                            final_output="Which account?", needs_input_node="a")
    provider, _ = _fake_node_llm("unused")
    with patch("durin.providers.factory.make_provider", return_value=provider), \
         patch("durin.workflow.engine.WorkflowEngine.run", return_value=paused):
        out = await tool.execute(name="asker", task="t", background=False)

    assert "No person can be asked" in out
    assert "ask_user_question" not in out


# ---------------------------------------------------------------------------
# The run summary: failures and the result come before the node list, and the
# node list is the only part a size limit shortens
# ---------------------------------------------------------------------------


class _RecordingBus:
    def __init__(self):
        self.inbound = []

    async def publish_inbound(self, msg):
        self.inbound.append(msg)


def _print_cmd(text):
    import shlex
    return f"printf %s {shlex.quote(text)}"


async def _run_summary(tmp_path, definition, agent_turn, *, background, workflow_config=None):
    """Run ``definition`` through the tool with the real engine and script runner
    (only the agent turn is faked) and return the summary the calling agent
    receives: the tool's return value in the foreground, the message delivered
    to the calling session in the background."""
    import asyncio

    _write_workflow(tmp_path, definition["name"], definition)
    bus = _RecordingBus()
    sessions = SessionManager(workspace=tmp_path)
    app_config = SimpleNamespace(resolve_default_preset=lambda: object(), tools=ToolsConfig(),
                                 workflow=workflow_config or WorkflowConfig())
    tool = RunWorkflowTool.create(SimpleNamespace(
        workspace=str(tmp_path), sessions=sessions, app_config=app_config, bus=bus))
    provider, _ = _fake_node_llm("unused")
    with patch("durin.providers.factory.make_provider", return_value=provider), \
         patch("durin.agent.runner.AgentRunner.run", AsyncMock(side_effect=agent_turn)):
        out = await tool.execute(name=definition["name"], task="go", background=background)
        if not background:
            return out
        async with asyncio.timeout(120):
            await asyncio.gather(*tool._bg_tasks)
    assert len(bus.inbound) == 1
    return bus.inbound[0].content


def _turn(output):
    return AgentRunResult(final_content=output, messages=[{"role": "assistant", "content": output}])


def _fan_out_definition(items, *, max_concurrency=4, report="echo 'the report is written'"):
    return {
        "name": "batch", "start": "list", "output": {"file": True},
        "nodes": [
            {"id": "list", "kind": "script", "command": _print_cmd(json.dumps(items)),
             "next": "fan"},
            {"id": "fan", "kind": "parallel", "worker": "research", "list_from": "list",
             "max_concurrency": max_concurrency, "next": "report"},
            {"id": "research", "kind": "work", "prompt": "research the item"},
            {"id": "report", "kind": "script",
             "command": f"echo notes > report.md; {report}", "next": None},
        ],
    }


def _quota_after(successes):
    """An agent turn that succeeds ``successes`` times, then fails every later
    call with the same provider error."""
    import threading

    lock = threading.Lock()
    state = {"calls": 0}

    async def turn(_spec):
        with lock:
            state["calls"] += 1
            n = state["calls"]
        if n > successes:
            raise RuntimeError("Usage limit reached for this plan. It resets at 18:00.")
        return _turn(f"finding {n}")

    return turn


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_summary_of_a_run_with_failed_workers_states_the_failures_once(tmp_path, background):
    items = [f"topic-{i}" for i in range(12)]
    text = await _run_summary(tmp_path, _fan_out_definition(items), _quota_after(2),
                              background=background)

    assert "completed" in text
    # the failed count, and the error: one line for ten identical failures
    assert "10 failed" in text
    assert text.count("Usage limit reached for this plan. It resets at 18:00.") == 1
    failure_line = next(line for line in text.splitlines() if "Usage limit reached" in line)
    assert "research" in failure_line and "10" in failure_line
    # the workers fold into one node line; the other nodes are still named
    assert sum(1 for line in text.splitlines() if line.lstrip().startswith("[research#")) == 1
    worker_line = next(line for line in text.splitlines() if line.lstrip().startswith("[research#"))
    assert "12" in worker_line and "10 failed" in worker_line
    for node_id in ("list", "fan", "report"):
        assert f"[{node_id}#1]" in text
    # failures come before the result, the result before the node list
    assert (text.index("Usage limit reached") < text.index("the report is written")
            < text.index("output files are in") < text.index("[list#1]"))


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_summary_of_a_small_successful_run_names_each_node(tmp_path, background):
    definition = {
        "name": "pair", "start": "draft",
        "nodes": [{"id": "draft", "kind": "work", "prompt": "p", "next": "polish"},
                  {"id": "polish", "kind": "work", "prompt": "p", "next": None}],
    }

    async def turn(_spec):
        return _turn("the polished answer")

    text = await _run_summary(tmp_path, definition, turn, background=background)
    assert "completed" in text
    assert "2 succeeded, 0 failed" in text
    assert "[draft#1]" in text and "[polish#1]" in text
    assert "the polished answer" in text
    assert "truncated" not in text.lower() and "not shown" not in text.lower()


@pytest.mark.asyncio
async def test_background_summary_of_a_long_run_keeps_the_result_and_the_folder(tmp_path):
    """A run with far more node records than the delivery limit has room for:
    the node list is what gets shortened, never the result or where its files are."""
    from durin.agent.tools.run_workflow import _MAX_INJECT_SUMMARY_CHARS

    passes = 130
    # A check that says "no" until the draft has been redone enough times.
    gate = (f'n=$(cat passes 2>/dev/null || echo 0); n=$((n+1)); echo $n > passes; '
            f'[ $n -ge {passes} ]')
    definition = {
        "name": "long", "start": "draft", "max_visits": passes + 5, "output": {"file": True},
        "nodes": [{"id": "draft", "kind": "work", "prompt": "p", "next": "check"},
                  {"id": "check", "kind": "script", "command": gate,
                   "on_pass": None, "on_fail": "draft"}],
    }

    async def turn(_spec):
        return _turn("the finished draft")

    text = await _run_summary(tmp_path, definition, turn, background=True,
                              workflow_config=WorkflowConfig(max_node_visits=passes + 5))

    assert "completed" in text
    assert f"{2 * passes} succeeded, 0 failed" in text
    assert "Final output:\nthe finished draft" in text
    work_dir = next(line for line in text.splitlines() if "output files are in" in line)
    assert str(tmp_path) in work_dir
    assert "passes" in text                                  # the folder's file is listed
    # the node list starts, is cut short, and says so
    assert "[draft#1]" in text and f"[check#{passes}]" not in text
    assert "not shown" in text
    assert len(text) < _MAX_INJECT_SUMMARY_CHARS + 1000


@pytest.mark.asyncio
async def test_background_summary_bounds_a_huge_final_output_and_keeps_the_folder(tmp_path):
    from durin.agent.tools.run_workflow import _MAX_INJECT_SUMMARY_CHARS

    definition = {
        "name": "wordy", "start": "dump", "output": {"file": True},
        "nodes": [{"id": "dump", "kind": "script",
                   "command": "echo notes > report.md; head -c 15000 /dev/zero | tr '\\0' x",
                   "next": None}],
    }

    async def turn(_spec):   # no agent node in this workflow
        raise AssertionError("unexpected agent turn")

    text = await _run_summary(tmp_path, definition, turn, background=True)

    assert "[Background workflow 'wordy' finished]" in text
    run_id = text.split("Workflow run ", 1)[1].split(":", 1)[0]
    assert "x" * 1000 in text                                 # the head of the output
    assert "x" * (_MAX_INJECT_SUMMARY_CHARS + 1) not in text  # but not all of it
    assert f"tasks(action='status', id='{run_id}')" in text   # where the rest is
    assert "output files are in" in text and "report.md" in text
    assert "[dump#1]" in text
    assert len(text) < _MAX_INJECT_SUMMARY_CHARS + 1000


def test_summary_lists_one_failure_line_per_distinct_error():
    result = WorkflowResult(
        status="completed", run_id="r1", final_output="merged",
        runs=[
            NodeRun(node_id="fetch", iteration=1, output="", worker_index=0,
                    status="node_failed", error="connection reset"),
            NodeRun(node_id="fetch", iteration=1, output="", worker_index=1,
                    status="node_failed", error="connection reset"),
            NodeRun(node_id="fetch", iteration=1, output="", worker_index=2,
                    status="node_failed", error="not found: item 7"),
            NodeRun(node_id="fetch", iteration=1, output="ok", worker_index=3,
                    session_key="workflow:r1:fetch:1:3"),
            NodeRun(node_id="fan", iteration=1, output="merged"),
        ],
    )
    text = _format_result(result)
    assert "2 succeeded, 3 failed" in text
    assert text.count("connection reset") == 1
    assert text.count("not found: item 7") == 1
    reset_line = next(line for line in text.splitlines() if "connection reset" in line)
    assert "2" in reset_line and "fetch" in reset_line


# ---------------------------------------------------------------------------
# The failures section is bounded: long errors are cut, many distinct errors
# end with a count of the rest
# ---------------------------------------------------------------------------


def _failing_after_first(error_for):
    """An agent turn whose first call succeeds and whose every later call fails
    with ``error_for(call number)``. Used with one worker at a time, so call n
    is the worker at index n - 1."""
    state = {"calls": 0}

    async def turn(_spec):
        state["calls"] += 1
        if state["calls"] == 1:
            return _turn("finding 1")
        raise RuntimeError(error_for(state["calls"]))

    return turn


@pytest.mark.asyncio
async def test_background_summary_of_forty_different_long_errors_stays_bounded(tmp_path):
    from durin.agent.tools.run_workflow import _MAX_INJECT_SUMMARY_CHARS

    def error_for(n):
        return f"request {n:03d} was rejected upstream: " + f"trace line for call {n}; " * 60

    # The worst case for size: the final output is over its own limit as well.
    definition = _fan_out_definition(
        [f"topic-{i}" for i in range(41)], max_concurrency=1,
        report="echo 'the report is written'; head -c 15000 /dev/zero | tr '\\0' x")
    text = await _run_summary(tmp_path, definition, _failing_after_first(error_for),
                              background=True)

    assert len(text) < 2 * _MAX_INJECT_SUMMARY_CHARS
    assert "4 succeeded, 40 failed" in text            # list, one worker, fan, report
    assert "request 002 was rejected upstream" in text  # the first failure's opening
    assert error_for(2) not in text                     # cut, not whole
    assert "request 041 was rejected upstream" not in text
    # the rest of the distinct errors are counted, with where to look
    closing = next(line for line in text.splitlines() if "distinct errors" in line)
    listed = sum(1 for line in text.splitlines() if "was rejected upstream" in line)
    assert f"{40 - listed} more distinct errors" in closing
    assert f"{40 - listed} failed runs" in closing
    run_id = text.split("Workflow run ", 1)[1].split(":", 1)[0]
    assert f"tasks(action='status', id='{run_id}')" in closing
    # the result and where its files are survive
    assert "the report is written" in text
    assert "output files are in" in text and "report.md" in text


@pytest.mark.asyncio
async def test_background_summary_of_forty_identical_errors_is_one_line_with_the_count(tmp_path):
    error = "Usage limit reached for this plan. It resets at 18:00."
    definition = _fan_out_definition([f"topic-{i}" for i in range(41)], max_concurrency=1)
    text = await _run_summary(tmp_path, definition, _failing_after_first(lambda _n: error),
                              background=True)

    assert "4 succeeded, 40 failed" in text
    assert text.count(error) == 1
    failure_line = next(line for line in text.splitlines() if error in line)
    assert "research" in failure_line and "40" in failure_line
    assert "distinct errors" not in text
    assert "the report is written" in text and "output files are in" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_summary_cuts_one_very_long_error_with_a_mark(tmp_path, background):
    error = "the upstream service answered with a page of HTML: " + "<div>filler</div>" * 400
    definition = _fan_out_definition(["topic-0", "topic-1"], max_concurrency=1)
    text = await _run_summary(tmp_path, definition, _failing_after_first(lambda _n: error),
                              background=background)

    assert "1 failed" in text
    failure_line = next(line for line in text.splitlines() if "the upstream service answered" in line)
    assert "research" in failure_line
    assert error not in text                             # not shown whole
    assert len(failure_line) < 1000
    assert "more characters" in failure_line            # and says it was cut, and by how much
    assert "the report is written" in text


def _aborted_doer(tmp_path, node):
    _write_workflow(tmp_path, "doer", {"name": "doer", "start": "only",
                                       "nodes": [{"id": "only", **node, "next": None}]})
    old = WorkflowResult(status="aborted", run_id="r1", final_output="boom", failed_node="only",
                         resume_upstream="",
                         runs=[NodeRun(node_id="only", iteration=1, output="", status="node_failed")])
    run_log.finalize_run(tmp_path, "doer", old, root_session_key=None,
                         started_at=1.0, finished_at=2.0, task="original task")
    return run_log.read_manifest(tmp_path, "doer", "r1")


@pytest.mark.asyncio
async def test_the_run_tool_loses_to_a_resume_that_claimed_first(tmp_path):
    before = _aborted_doer(tmp_path, {"kind": "script", "command": "echo ok"})
    tool = _tool(tmp_path)

    def _provider_while_another_resume_claims(*_a, **_kw):
        ResumeClaim.take(tmp_path, "doer", "r1", expected=before)
        fake = MagicMock(spec=LLMProvider)
        fake.get_default_model.return_value = "m"
        return fake

    with patch("durin.providers.factory.make_provider",
               side_effect=_provider_while_another_resume_claims), \
         patch("durin.workflow.engine.WorkflowEngine.run") as fake_run:
        out = await tool.execute(name="doer", task="", resume_run_id="r1", background=False)
    assert out.startswith("Error:") and "already resumed" in out
    fake_run.assert_not_called()


@pytest.mark.asyncio
async def test_the_run_tool_gives_the_record_back_when_its_resume_never_walks(tmp_path):
    before = _aborted_doer(tmp_path, {"kind": "script", "script": "gone.sh"})
    fake = MagicMock(spec=LLMProvider)
    fake.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake):
        out = await _tool(tmp_path).execute(name="doer", task="", resume_run_id="r1", background=False)
    assert "script file not found" in out
    assert run_log.read_manifest(tmp_path, "doer", "r1") == before


@pytest.mark.asyncio
async def test_the_run_tool_gives_the_record_back_when_its_background_resume_is_cancelled_before_it_starts(tmp_path):
    import asyncio

    before = _aborted_doer(tmp_path, {"kind": "script", "command": "echo ok"})
    fake = MagicMock(spec=LLMProvider)
    fake.get_default_model.return_value = "m"
    tool = _tool(tmp_path)
    with patch("durin.providers.factory.make_provider", return_value=fake), \
         patch("durin.workflow.engine.WorkflowEngine.run") as fake_run:
        await tool.execute(name="doer", task="", resume_run_id="r1", background=True)
        assert run_log.read_manifest(tmp_path, "doer", "r1")["status"] == "running"
        pending = list(tool._bg_tasks)
        for t in pending:
            t.cancel()   # before the task ever ran a step
        await asyncio.gather(*pending, return_exceptions=True)
    fake_run.assert_not_called()
    assert run_log.read_manifest(tmp_path, "doer", "r1") == before


# A pid far above any real pid_max, so the owner reads as long gone.
_DEAD_OWNER = {"pid": 2**22 + 54321, "started": "never"}


def _dead_doer(tmp_path, node, *, root_session_key=None, launch=None):
    """Run ``r1`` of workflow ``doer`` left ``running`` by a process that died in
    its only step, with that step's input stored and its folder in place."""
    from durin.workflow import resume_store
    from durin.workflow.artifacts import artifact_dir

    _write_workflow(tmp_path, "doer", {"name": "doer", "start": "only",
                                       "nodes": [{"id": "only", **node, "next": None}]})
    work_dir = artifact_dir(tmp_path, "r1", "work", None)
    work_dir.mkdir(parents=True, exist_ok=True)
    resume_store.save_input(resume_store.store_dir(tmp_path, "r1"), 1, "only", "")
    run_log.start_run(tmp_path, "doer", "r1", root_session_key=root_session_key,
                      started_at=1.0, task="original task", work_dir=str(work_dir),
                      position={"node": "only", "seq": 1}, surface="agent", launch=launch)
    f = tmp_path / "workflows-runs" / "doer" / "r1.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec.update(owner=_DEAD_OWNER, active_node={
        "node_id": "only", "label": "only", "started_at": 1.0,
        "iteration": 1, "session_key": None})
    f.write_text(json.dumps(rec), encoding="utf-8")


@pytest.mark.asyncio
async def test_the_run_tool_resumes_a_run_whose_process_died(tmp_path):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"})
    fake = MagicMock(spec=LLMProvider)
    fake.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake):
        out = await _tool(tmp_path).execute(name="doer", task="", resume_run_id="r1",
                                            background=False)
    assert "completed" in out.lower() and "again" in out
    rec = run_log.read_manifest(tmp_path, "doer", "r1")
    assert rec["status"] == "completed" and rec["surface"] == "agent"
    assert [(r["node_id"], r["iteration"], bool(r.get("cut")))
            for r in rec["history"] + rec["runs"]] == [("only", 1, True), ("only", 2, False)]


@pytest.mark.asyncio
async def test_the_run_tool_refuses_a_dead_run_an_automation_started(tmp_path):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="automation:nightly")
    with patch("durin.workflow.engine.WorkflowEngine.run") as fake_run:
        out = await _tool(tmp_path).execute(name="doer", task="", resume_run_id="r1",
                                            background=False)
    assert out == ("Error: run 'r1' cannot be resumed — it was started by an automation, "
                   "and a run an automation started is not resumed by hand: the automation "
                   "does not resume it, and its next run starts anew.")
    fake_run.assert_not_called()
    assert run_log.read_manifest(tmp_path, "doer", "r1")["status"] == "crashed"


@pytest.mark.asyncio
async def test_the_run_tool_loses_a_dead_run_to_a_resume_that_claimed_first(tmp_path):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo ok"})
    tool = _tool(tmp_path)

    def _provider_while_another_resume_claims(*_a, **_kw):
        # By now this call converted the dead run; another resume claims it.
        converted = run_log.read_manifest(tmp_path, "doer", "r1")
        assert converted["status"] == "crashed"
        ResumeClaim.take(tmp_path, "doer", "r1", expected=converted)
        fake = MagicMock(spec=LLMProvider)
        fake.get_default_model.return_value = "m"
        return fake

    with patch("durin.providers.factory.make_provider",
               side_effect=_provider_while_another_resume_claims), \
         patch("durin.workflow.engine.WorkflowEngine.run") as fake_run:
        out = await tool.execute(name="doer", task="", resume_run_id="r1", background=False)
    assert out.startswith("Error:") and "already resumed" in out
    fake_run.assert_not_called()


@pytest.mark.asyncio
async def test_the_run_tool_refuses_a_run_whose_process_is_alive(tmp_path):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo ok"})
    f = tmp_path / "workflows-runs" / "doer" / "r1.json"
    from durin.utils.process_tree import process_identity
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec["owner"] = process_identity()       # this live process: a run in flight
    f.write_text(json.dumps(rec), encoding="utf-8")
    before = f.read_bytes()
    with patch("durin.workflow.engine.WorkflowEngine.run") as fake_run:
        out = await _tool(tmp_path).execute(name="doer", task="", resume_run_id="r1",
                                            background=False)
    assert "has status 'running' and cannot be resumed" in out
    fake_run.assert_not_called()
    assert f.read_bytes() == before


@pytest.mark.asyncio
async def test_a_run_the_tool_launches_is_recorded_as_the_agents(tmp_path):
    _write_workflow(tmp_path, "scripted", {
        "name": "scripted", "start": "s",
        "nodes": [{"id": "s", "kind": "script", "command": "echo tool-ok", "next": None}],
    })
    fake = MagicMock(spec=LLMProvider)
    fake.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake):
        await _tool(tmp_path).execute(name="scripted", task="go", background=False)
    [entry] = run_log.list_runs(tmp_path, "scripted")
    assert run_log.read_manifest(tmp_path, "scripted", entry["run_id"])["surface"] == "agent"


# --- where the run was launched from ----------------------------------------


def _context(tool, channel, chat_id, session_key):
    from durin.agent.tools.context import RequestContext

    tool.set_context(RequestContext(channel=channel, chat_id=chat_id, session_key=session_key))


def _script_workflow(tmp_path):
    _write_workflow(tmp_path, "doer", {"name": "doer", "start": "only", "nodes": [
        {"id": "only", "kind": "script", "command": "echo ok", "next": None}]})


@pytest.mark.asyncio
async def test_a_launch_records_the_conversation_it_came_from(tmp_path):
    _script_workflow(tmp_path)
    tool = _tool(tmp_path)
    _context(tool, "websocket", "chatA", "websocket:chatA")
    with patch("durin.providers.factory.make_provider", return_value=MagicMock(spec=LLMProvider)):
        out = await tool.execute(name="doer", task="go", background=False)
    run_id = out.split("Workflow run ", 1)[1].split(":", 1)[0]
    assert run_log.read_manifest(tmp_path, "doer", run_id)["launch"] == {
        "kind": "session", "session_key": "websocket:chatA", "channel": "websocket",
        "chat_id": "chatA"}


@pytest.mark.asyncio
async def test_a_launch_without_channel_or_chat_records_the_defaults(tmp_path):
    _script_workflow(tmp_path)
    tool = _tool(tmp_path)
    _context(tool, "", "", "unified:default")
    with patch("durin.providers.factory.make_provider", return_value=MagicMock(spec=LLMProvider)):
        out = await tool.execute(name="doer", task="go", background=False)
    run_id = out.split("Workflow run ", 1)[1].split(":", 1)[0]
    assert run_log.read_manifest(tmp_path, "doer", run_id)["launch"] == {
        "kind": "session", "session_key": "unified:default", "channel": "websocket",
        "chat_id": ""}


@pytest.mark.asyncio
async def test_a_launch_with_no_calling_session_records_none(tmp_path):
    _script_workflow(tmp_path)
    with patch("durin.providers.factory.make_provider", return_value=MagicMock(spec=LLMProvider)):
        out = await _tool(tmp_path).execute(name="doer", task="go", background=False)
    run_id = out.split("Workflow run ", 1)[1].split(":", 1)[0]
    assert run_log.read_manifest(tmp_path, "doer", run_id).get("launch") is None


@pytest.mark.asyncio
async def test_a_resume_from_another_conversation_keeps_the_launch(tmp_path):
    first = {"kind": "session", "session_key": "websocket:chatA", "channel": "websocket",
             "chat_id": "chatA"}
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="websocket:chatA", launch=first)
    tool = _tool(tmp_path)
    _context(tool, "slack", "C9", "slack:C9")
    fake = MagicMock(spec=LLMProvider)
    fake.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake):
        out = await tool.execute(name="doer", task="", resume_run_id="r1", background=False)
    assert "completed" in out.lower()
    rec = run_log.read_manifest(tmp_path, "doer", "r1")
    assert rec["root_session_key"] == "slack:C9"
    assert rec["launch"] == first


@pytest.mark.asyncio
async def test_the_run_tool_refuses_a_dead_run_launched_by_an_automation_under_any_root(tmp_path):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="websocket:x",
               launch={"kind": "automation", "name": "nightly"})
    with patch("durin.workflow.engine.WorkflowEngine.run") as fake_run:
        out = await _tool(tmp_path).execute(name="doer", task="", resume_run_id="r1",
                                            background=False)
    assert out == ("Error: run 'r1' cannot be resumed — it was started by an automation, "
                   "and a run an automation started is not resumed by hand: the automation "
                   "does not resume it, and its next run starts anew.")
    fake_run.assert_not_called()


# --- a resumed run reports to the conversation that launched it -------------

_LAUNCHED_IN_A = {"kind": "session", "session_key": "websocket:chatA", "channel": "websocket",
                  "chat_id": "chatA"}
_RESUMED_ELSEWHERE = ("[Workflow 'doer' run r1, started from this conversation, was resumed "
                      "from another conversation]\n\n")


def _tool_on_bus(tmp_path, bus, *, channel, chat_id, session_key, serves=("websocket", "slack")):
    """A tool in a process that serves the channels named in ``serves``."""
    sessions = SessionManager(workspace=tmp_path)
    app_config = SimpleNamespace(resolve_default_preset=lambda: object(), tools=ToolsConfig(),
                                 workflow=WorkflowConfig())
    tool = RunWorkflowTool.create(SimpleNamespace(
        workspace=str(tmp_path), sessions=sessions, app_config=app_config, bus=bus,
        serves_channel=lambda name: name in serves))
    _context(tool, channel, chat_id, session_key)
    return tool


async def _resume_through_the_tool(tool, *, background):
    import asyncio

    fake = MagicMock(spec=LLMProvider)
    fake.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake):
        out = await tool.execute(name="doer", task="", resume_run_id="r1", background=background)
        async with asyncio.timeout(60):
            await asyncio.gather(*tool._bg_tasks)
    return out


@pytest.mark.asyncio
async def test_a_background_resume_from_another_conversation_reports_to_both(tmp_path):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="websocket:chatA", launch=_LAUNCHED_IN_A)
    bus = _RecordingBus()
    tool = _tool_on_bus(tmp_path, bus, channel="websocket", chat_id="chatB",
                        session_key="websocket:chatB")
    await _resume_through_the_tool(tool, background=True)

    by_session = {m.session_key_override: m for m in bus.inbound}
    assert len(bus.inbound) == 2 and set(by_session) == {"websocket:chatA", "websocket:chatB"}
    assert by_session["websocket:chatB"].content.startswith("[Background workflow 'doer' finished]")
    to_a = by_session["websocket:chatA"]
    assert to_a.content.startswith(_RESUMED_ELSEWHERE) and "again" in to_a.content
    assert to_a.chat_id == "websocket:chatA"
    assert to_a.metadata == {"injected_event": "workflow_background_result", "workflow": "doer"}


@pytest.mark.asyncio
async def test_a_background_resume_from_the_launch_conversation_reports_once(tmp_path):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="websocket:chatA", launch=_LAUNCHED_IN_A)
    bus = _RecordingBus()
    tool = _tool_on_bus(tmp_path, bus, channel="websocket", chat_id="chatA",
                        session_key="websocket:chatA")
    await _resume_through_the_tool(tool, background=True)

    [msg] = bus.inbound
    assert msg.session_key_override == "websocket:chatA"
    assert msg.content.startswith("[Background workflow 'doer' finished]")


@pytest.mark.asyncio
async def test_a_foreground_resume_from_another_conversation_reports_to_the_launch_one(tmp_path):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="websocket:chatA", launch=_LAUNCHED_IN_A)
    bus = _RecordingBus()
    tool = _tool_on_bus(tmp_path, bus, channel="slack", chat_id="C9", session_key="slack:C9")
    out = await _resume_through_the_tool(tool, background=False)

    assert "completed" in out.lower()
    [msg] = bus.inbound
    assert msg.session_key_override == "websocket:chatA"
    assert msg.content.startswith(_RESUMED_ELSEWHERE)


@pytest.mark.asyncio
async def test_a_foreground_resume_from_the_launch_conversation_publishes_nothing(tmp_path):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="websocket:chatA", launch=_LAUNCHED_IN_A)
    bus = _RecordingBus()
    tool = _tool_on_bus(tmp_path, bus, channel="websocket", chat_id="chatA",
                        session_key="websocket:chatA")
    out = await _resume_through_the_tool(tool, background=False)

    assert "completed" in out.lower()
    assert bus.inbound == []


@pytest.mark.asyncio
async def test_a_resume_of_a_record_without_a_launch_reports_as_before(tmp_path):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="websocket:chatA")
    bus = _RecordingBus()
    tool = _tool_on_bus(tmp_path, bus, channel="websocket", chat_id="chatB",
                        session_key="websocket:chatB")
    await _resume_through_the_tool(tool, background=True)

    [msg] = bus.inbound
    assert msg.session_key_override == "websocket:chatB"


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_a_refused_resume_reports_to_no_conversation(tmp_path, monkeypatch, background):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="websocket:chatA", launch=_LAUNCHED_IN_A)
    settle = run_log.settle_dead_run

    def _a_script_is_still_running(*args):
        record, _ = settle(*args)
        return record, "the script of its step 'only' from the earlier process is still running"

    monkeypatch.setattr(run_log, "settle_dead_run", _a_script_is_still_running)
    bus = _RecordingBus()
    tool = _tool_on_bus(tmp_path, bus, channel="websocket", chat_id="chatB",
                        session_key="websocket:chatB")
    out = await _resume_through_the_tool(tool, background=background)

    assert out.startswith("Error:") and "still running" in out
    assert bus.inbound == []


@pytest.mark.asyncio
async def test_a_resume_of_a_step_gone_from_the_definition_reports_to_no_conversation(tmp_path):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="websocket:chatA", launch=_LAUNCHED_IN_A)
    _write_workflow(tmp_path, "doer", {"name": "doer", "start": "other", "nodes": [
        {"id": "other", "kind": "script", "command": "echo x", "next": None}]})
    bus = _RecordingBus()
    tool = _tool_on_bus(tmp_path, bus, channel="websocket", chat_id="chatB",
                        session_key="websocket:chatB")
    out = await _resume_through_the_tool(tool, background=True)

    assert out.startswith("Error:")
    assert bus.inbound == []


@pytest.mark.asyncio
async def test_a_resume_that_loses_the_claim_reports_to_no_conversation(tmp_path):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="websocket:chatA", launch=_LAUNCHED_IN_A)
    bus = _RecordingBus()
    tool = _tool_on_bus(tmp_path, bus, channel="websocket", chat_id="chatB",
                        session_key="websocket:chatB")

    def _provider_while_another_resume_claims(*_a, **_kw):
        ResumeClaim.take(tmp_path, "doer", "r1",
                         expected=run_log.read_manifest(tmp_path, "doer", "r1"))
        fake = MagicMock(spec=LLMProvider)
        fake.get_default_model.return_value = "m"
        return fake

    with patch("durin.providers.factory.make_provider",
               side_effect=_provider_while_another_resume_claims):
        out = await tool.execute(name="doer", task="", resume_run_id="r1", background=True)
    assert out.startswith("Error:") and "already resumed" in out
    assert bus.inbound == []


@pytest.mark.asyncio
async def test_a_background_resume_given_up_before_its_walk_reports_to_no_conversation(tmp_path):
    import asyncio

    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="websocket:chatA", launch=_LAUNCHED_IN_A)
    bus = _RecordingBus()
    tool = _tool_on_bus(tmp_path, bus, channel="websocket", chat_id="chatB",
                        session_key="websocket:chatB")
    fake = MagicMock(spec=LLMProvider)
    fake.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake), \
         patch("durin.workflow.engine.WorkflowEngine.run") as fake_run:
        await tool.execute(name="doer", task="", resume_run_id="r1", background=True)
        pending = list(tool._bg_tasks)
        for t in pending:
            t.cancel()   # before the task ever ran a step
        async with asyncio.timeout(30):
            await asyncio.gather(*pending, return_exceptions=True)
    fake_run.assert_not_called()
    assert run_log.read_manifest(tmp_path, "doer", "r1")["status"] == "crashed"
    assert bus.inbound == []


@pytest.mark.asyncio
async def test_a_foreground_resume_rejected_before_its_walk_reports_to_no_other_conversation(
        tmp_path):
    _dead_doer(tmp_path, {"kind": "script", "script": "gone.sh"},
               root_session_key="websocket:chatA", launch=_LAUNCHED_IN_A)
    bus = _RecordingBus()
    tool = _tool_on_bus(tmp_path, bus, channel="websocket", chat_id="chatB",
                        session_key="websocket:chatB")
    out = await _resume_through_the_tool(tool, background=False)

    assert "script file not found" in out
    assert run_log.read_manifest(tmp_path, "doer", "r1")["status"] == "crashed"
    assert bus.inbound == []


@pytest.mark.asyncio
async def test_a_foreground_walk_that_raises_reports_the_failure_to_the_launch_conversation(
        tmp_path):
    from durin.workflow.engine import WorkflowConfigError

    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="websocket:chatA", launch=_LAUNCHED_IN_A)
    bus = _RecordingBus()
    tool = _tool_on_bus(tmp_path, bus, channel="websocket", chat_id="chatB",
                        session_key="websocket:chatB")
    with patch("durin.workflow.engine.WorkflowEngine._walk",
               side_effect=WorkflowConfigError("no runner for it")):
        with pytest.raises(WorkflowConfigError):
            await _resume_through_the_tool(tool, background=False)

    [msg] = bus.inbound
    assert msg.session_key_override == "websocket:chatA"
    assert msg.content.startswith(_RESUMED_ELSEWHERE + "Workflow run failed: no runner for it")


@pytest.mark.asyncio
async def test_a_background_walk_that_raises_reports_the_failure_to_both_conversations(tmp_path):
    from durin.workflow.engine import WorkflowConfigError

    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="websocket:chatA", launch=_LAUNCHED_IN_A)
    bus = _RecordingBus()
    tool = _tool_on_bus(tmp_path, bus, channel="websocket", chat_id="chatB",
                        session_key="websocket:chatB")
    with patch("durin.workflow.engine.WorkflowEngine._walk",
               side_effect=WorkflowConfigError("no runner for it")):
        await _resume_through_the_tool(tool, background=True)

    by_session = {m.session_key_override: m.content for m in bus.inbound}
    assert len(bus.inbound) == 2
    assert "Workflow run failed in background: no runner for it" in by_session["websocket:chatB"]
    assert by_session["websocket:chatA"].startswith(
        _RESUMED_ELSEWHERE + "Workflow run failed: no runner for it")


_LAUNCHED_IN_SLACK = {"kind": "session", "session_key": "slack:C1:1700.1", "channel": "slack",
                      "chat_id": "C1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_a_process_that_does_not_serve_the_launch_channel_reports_nothing_there(
        tmp_path, background):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="slack:C1:1700.1", launch=_LAUNCHED_IN_SLACK)
    bus = _RecordingBus()
    # The terminal UI's process: it runs no slack channel.
    tool = _tool_on_bus(tmp_path, bus, channel="cli", chat_id="direct", session_key="cli:direct",
                        serves=())
    out = await _resume_through_the_tool(tool, background=background)

    assert all(m.session_key_override != "slack:C1:1700.1" for m in bus.inbound)
    if background:
        [msg] = bus.inbound
        assert msg.session_key_override == "cli:direct"
        assert msg.content.startswith("[Background workflow 'doer' finished]")
    else:
        assert "completed" in out.lower()
        assert bus.inbound == []


@pytest.mark.asyncio
async def test_a_process_that_serves_the_launch_channel_reports_there(tmp_path):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="slack:C1:1700.1", launch=_LAUNCHED_IN_SLACK)
    bus = _RecordingBus()
    tool = _tool_on_bus(tmp_path, bus, channel="websocket", chat_id="chatB",
                        session_key="websocket:chatB", serves=("websocket", "slack"))
    await _resume_through_the_tool(tool, background=False)

    [msg] = bus.inbound
    assert (msg.chat_id, msg.session_key_override) == ("slack:C1", "slack:C1:1700.1")
    assert msg.content.startswith(
        "[Workflow 'doer' run r1, started from this conversation, was resumed from another "
        "conversation]\n\n")


# ---------------------------------------------------------------------------
# A fresh launch names the runs of that workflow the same conversation had cut.
# ---------------------------------------------------------------------------

_HERE = {"kind": "session", "session_key": "websocket:chatA",
         "channel": "websocket", "chat_id": "chatA"}


def _cut(tmp_path, run_id, *, started_at, launch=_HERE, root="websocket:chatA", node="build",
         parent=None, owner=_DEAD_OWNER, status="running", keep_folder=True, incomplete=False,
         key=None):
    """Run ``run_id`` of workflow ``qa``, started from a conversation and left by
    a process that died in step ``node`` (``status`` ``running`` with a dead
    owner, or settled to ``crashed`` already); on work key ``key`` when given."""
    from durin.workflow import resume_store
    from durin.workflow.artifacts import artifact_dir, keyed_work_dir

    _write_workflow(tmp_path, "qa", {"name": "qa", "start": "build", "nodes": [
        {"id": "build", "kind": "script", "command": "echo ok", "next": None}]})
    work_dir = (keyed_work_dir(tmp_path, "qa", key) if key
                else artifact_dir(tmp_path, run_id, "work", None))
    work_dir.mkdir(parents=True, exist_ok=True)
    resume_store.save_input(resume_store.store_dir(tmp_path, run_id, work_dir=str(work_dir),
                                                   keyed=key is not None), 1, node, "")
    run_log.start_run(tmp_path, "qa", run_id, root_session_key=root, started_at=started_at,
                      task="t", work_dir=str(work_dir), work_key=key,
                      position={"node": node, "seq": 1},
                      surface="agent", launch=launch, parent_run_id=parent)
    f = tmp_path / "workflows-runs" / "qa" / f"{run_id}.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec.update(owner=owner, active_node={
        "node_id": node, "label": node, "started_at": started_at,
        "iteration": 1, "session_key": None})
    if incomplete:
        rec["incomplete"] = True
    f.write_text(json.dumps(rec), encoding="utf-8")
    if not keep_folder:
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)
    if status == "crashed":
        run_log.mark_dead_run(tmp_path, "qa", run_id)


def _fresh_result(*_a, **_kw):
    return WorkflowResult(status="completed", run_id="fresh", final_output="ok", runs=[])


async def _launch(tmp_path, *, session="websocket:chatA", background=True, resume_run_id="",
                  work_key=""):
    import asyncio

    from durin.agent.tools.context import RequestContext

    tool = _tool(tmp_path)
    if session is not None:
        channel, chat_id = session.split(":", 1)
        tool.set_context(RequestContext(channel=channel, chat_id=chat_id, session_key=session))
    fake = MagicMock(spec=LLMProvider)
    fake.get_default_model.return_value = "m"

    def fake_run(self, workflow, task, *, root_session_key=None, input_files=None,
                 output_format=None, resume=None, work_dir_override=None, parent_run_id=None,
                 work_key=None, launch=None):
        return _fresh_result()

    with patch("durin.providers.factory.make_provider", return_value=fake), \
         patch("durin.workflow.engine.WorkflowEngine.run", fake_run):
        out = await tool.execute(name="qa", task="go", background=background,
                                 resume_run_id=resume_run_id, work_key=work_key)
        async with asyncio.timeout(60):
            await asyncio.gather(*tool._bg_tasks)
    return out


def _resumable_text(run_id, node="build"):
    return (f"Note: run {run_id} of 'qa', started from this conversation, was cut short when "
            f"its process ended and has not been resumed. It can be resumed with "
            f"resume_run_id='{run_id}' (it enters node '{node}' again; finished nodes do not "
            "run again). This call did not resume it.")


_ON_THE_KEY = ("It is on the same work key as this call: once this call's run starts, it "
               "takes over that folder")


def _same_key_text(run_id="r-cut"):
    return (f"Note: run {run_id} of 'qa', started from this conversation, was cut short when "
            f"its process ended. {_ON_THE_KEY} and that run can no longer be resumed.")


@pytest.mark.asyncio
async def test_a_launch_names_a_run_this_conversation_had_cut(tmp_path):
    _cut(tmp_path, "r-cut", started_at=1.0, status="crashed")
    reply = await _launch(tmp_path)
    assert ("Note: run r-cut of 'qa', started from this conversation, was cut short when its "
            "process ended and has not been resumed.") in reply
    assert "resume_run_id='r-cut'" in reply and "it enters node 'build' again" in reply
    assert reply.rstrip().endswith("This call did not resume it.")
    assert reply.count("\n\nNote: run r-cut") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [True, False])
async def test_a_launch_on_the_cut_runs_own_key_says_it_takes_the_folder_over(
        tmp_path, background):
    # A turn replayed after a restart launches the workflow again on the same key.
    _cut(tmp_path, "r-cut", started_at=1.0, status="crashed", key="k1")
    reply = await _launch(tmp_path, work_key="k1", background=background)
    assert reply.endswith("\n\n" + _same_key_text())
    # What the note says is not a resume to attempt: it names no way to resume.
    assert "resume_run_id=" not in reply.split("Note:")[1]


@pytest.mark.asyncio
async def test_the_same_key_note_holds_when_the_launch_is_refused_before_its_run_starts(
        tmp_path):
    from durin.agent.tools.context import RequestContext
    from durin.workflow.engine import resume_refusal

    _cut(tmp_path, "r-cut", started_at=1.0, status="crashed", key="k1")
    tool = _tool(tmp_path)
    tool.set_context(RequestContext(channel="websocket", chat_id="chatA",
                                    session_key="websocket:chatA"))
    fake = MagicMock(spec=LLMProvider)
    fake.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake):
        reply = await tool.execute(name="qa", task="go", background=False, work_key="k1",
                                   input_files=[str(tmp_path / "missing.txt")])
    assert "input file not found" in reply
    assert reply.endswith("\n\n" + _same_key_text())
    # The refused launch recorded no run: the cut run is still resumable, and the
    # note did not say otherwise.
    assert [r["run_id"] for r in run_log.list_runs(tmp_path, "qa")] == ["r-cut"]
    rec = run_log.read_manifest(tmp_path, "qa", "r-cut")
    assert resume_refusal(rec, workspace=tmp_path) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cut_key,launch_key", [("k1", "k2"), ("k1", ""), (None, "k2")])
async def test_a_launch_on_another_key_names_the_cut_run_as_resumable(
        tmp_path, cut_key, launch_key):
    _cut(tmp_path, "r-cut", started_at=1.0, status="crashed", key=cut_key)
    reply = await _launch(tmp_path, work_key=launch_key)
    assert reply.endswith("\n\n" + _resumable_text("r-cut"))


@pytest.mark.asyncio
@pytest.mark.parametrize("resumable", [True, False])
async def test_a_launch_on_the_key_of_a_cut_run_with_something_still_there_names_both(
        tmp_path, monkeypatch, resumable):
    _cut(tmp_path, "r-cut", started_at=1.0, status="crashed", key="k1",
         **({} if resumable else {"node": "removed"}))
    fact = _LEFT_BEHIND["script"](tmp_path, monkeypatch)
    reply = await _launch(tmp_path, work_key="k1")
    head = "Note: run r-cut of 'qa', started from this conversation, was cut short and "
    if resumable:
        expected = head + f"{fact}. {_ON_THE_KEY} and that run can no longer be resumed."
    else:
        expected = head + f"cannot be resumed, and {fact}. {_ON_THE_KEY}."
    assert reply.endswith("\n\n" + expected)
    assert "resume_run_id" not in reply.split("Note:")[1]


@pytest.mark.asyncio
async def test_a_launch_with_nothing_cut_is_the_plain_launch_reply(tmp_path):
    from durin.agent.tools.run_workflow import _background_launch_message
    _write_workflow(tmp_path, "qa", {"name": "qa", "start": "build", "nodes": [
        {"id": "build", "kind": "script", "command": "echo ok", "next": None}]})
    reply = await _launch(tmp_path)
    run_id = reply.split("run id: ")[1].split(")")[0]
    assert reply == _background_launch_message("qa", run_id)


@pytest.mark.asyncio
async def test_a_running_record_whose_process_is_gone_is_settled_and_named(tmp_path):
    _cut(tmp_path, "r-cut", started_at=1.0)
    assert run_log.read_manifest(tmp_path, "qa", "r-cut")["status"] == "running"
    reply = await _launch(tmp_path)
    assert _resumable_text("r-cut") in reply
    assert run_log.read_manifest(tmp_path, "qa", "r-cut")["status"] == "crashed"


@pytest.mark.asyncio
async def test_a_run_another_conversation_started_is_not_named(tmp_path):
    other = {**_HERE, "session_key": "websocket:chatB", "chat_id": "chatB"}
    _cut(tmp_path, "r-cut", started_at=1.0, launch=other, root="websocket:chatB",
         status="crashed")
    assert "Note:" not in await _launch(tmp_path)


@pytest.mark.asyncio
async def test_a_live_run_a_finished_run_and_a_child_run_are_not_named(tmp_path):
    from durin.utils.process_tree import process_identity
    _cut(tmp_path, "r-live", started_at=1.0, owner=process_identity())
    _cut(tmp_path, "r-kid", started_at=2.0, parent="r-top", status="crashed")
    _cut(tmp_path, "r-done", started_at=3.0, status="crashed")
    f = tmp_path / "workflows-runs" / "qa" / "r-done.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec.update(status="completed", failed_node=None)
    f.write_text(json.dumps(rec), encoding="utf-8")
    reply = await _launch(tmp_path)
    assert "Note:" not in reply
    assert run_log.read_manifest(tmp_path, "qa", "r-live")["status"] == "running"


@pytest.mark.asyncio
async def test_a_cut_run_whose_check_answers_with_a_race_refusal_is_not_named(tmp_path):
    _cut(tmp_path, "r-cut", started_at=1.0, status="crashed")
    busy = sorted(run_log.DEAD_RUN_RACE_REFUSALS)[0]
    with patch.object(run_log, "dead_run_refusal", return_value=busy):
        reply = await _launch(tmp_path)
    assert "Note:" not in reply


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["step_gone", "folder_pruned", "incomplete"])
async def test_a_cut_run_refused_for_another_reason_is_not_named(tmp_path, how):
    kwargs = {"step_gone": {"node": "removed"}, "folder_pruned": {"keep_folder": False},
              "incomplete": {"incomplete": True}}[how]
    _cut(tmp_path, "r-cut", started_at=1.0, status="crashed", **kwargs)
    assert "Note:" not in await _launch(tmp_path)


def _still_there_text(fact, *, resumable, run_id="r-cut"):
    head = f"Note: run {run_id} of 'qa', started from this conversation, was cut short and "
    if resumable:
        return (head + f"{fact}. It can be resumed once that is gone. "
                "This call did not resume it; a new run would work beside what it left.")
    return (head + f"cannot be resumed, and {fact}. "
            "This call did not resume it; a new run would work beside what it left.")


def _leave_a_script_record(tmp_path, monkeypatch, state, own, group, *, run_id="r-cut"):
    from durin.utils import process_tree
    run_log.record_script(tmp_path / "workflows-runs" / "qa", run_id, pid=424242,
                          started="x", node_id="build")
    monkeypatch.setattr(process_tree, "left_script_state",
                        lambda pid, started, **kw: (state, own, group))


def _leave_a_sub_run(tmp_path, monkeypatch):
    run_log.start_run(tmp_path, "kid", "r-kid", root_session_key="websocket:chatA",
                      started_at=2.0, parent_run_id="r-cut")
    return "its sub-workflow run 'r-kid' of workflow 'kid' is still recorded as running"


def _leave_a_live_owner(tmp_path, monkeypatch):
    from durin.utils.process_tree import process_identity
    f = tmp_path / "workflows-runs" / "qa" / "r-cut.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec["owner"] = process_identity()
    f.write_text(json.dumps(rec), encoding="utf-8")
    return f"the process that was running it (pid {process_identity()['pid']}) still seems alive"


def _leave_an_unreadable_record(tmp_path, monkeypatch):
    path = tmp_path / "workflows-runs" / "qa" / "r-cut.script.9"
    path.write_text("{not json", encoding="utf-8")
    return (f"a record of one of its scripts, {path}, cannot be read, so whether that "
            "script still runs cannot be checked")


def _script_fact(state, own, group, fact):
    def leave(tmp_path, monkeypatch):
        _leave_a_script_record(tmp_path, monkeypatch, state, own, group)
        return fact
    return leave


_LEFT_BEHIND = {
    "script_and_group": _script_fact(
        "alive", [111], [222],
        "the script of its step 'build' is still running (pid 111), and so are processes "
        "in its former process group (pid 222), which may not be that script's"),
    "script": _script_fact("alive", [111], [],
                           "the script of its step 'build' is still running (pid 111)"),
    "group": _script_fact(
        "alive", [], [222],
        "processes in the former process group of the script of its step 'build' are still "
        "running (pid 222), which may not be that script's"),
    "unknown": _script_fact(
        "unknown", [], [222],
        "whether the script of its step 'build' ended could not be checked (pid 222)"),
    "unreadable": _leave_an_unreadable_record,
    "owner": _leave_a_live_owner,
    "sub_run": _leave_a_sub_run,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("resumable", [True, False])
@pytest.mark.parametrize("kind", sorted(_LEFT_BEHIND))
async def test_a_cut_run_with_something_still_there_is_named_with_that_fact(
        tmp_path, monkeypatch, kind, resumable):
    _cut(tmp_path, "r-cut", started_at=1.0, status="crashed",
         **({} if resumable else {"node": "removed"}))
    fact = _LEFT_BEHIND[kind](tmp_path, monkeypatch)
    reply = await _launch(tmp_path)
    assert _still_there_text(fact, resumable=resumable) in reply
    assert "resume_run_id" not in reply.split("Note:")[1]
    # the refusal a resume gets keeps its own words
    reason = run_log.settle_dead_run(tmp_path, "qa", "r-cut")[1]
    assert reason.fact == fact and "It can be resumed once" in reason


@pytest.mark.asyncio
@pytest.mark.parametrize("resumable", [True, False])
async def test_a_cut_run_whose_script_is_really_still_running_is_named(tmp_path, resumable):
    import os
    import signal
    import subprocess

    from durin.utils.process_tree import process_identity

    _cut(tmp_path, "r-cut", started_at=1.0, **({} if resumable else {"node": "removed"}))
    proc = subprocess.Popen(["sh", "-c", "sleep 61 & echo $!; read _"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True, start_new_session=True)
    member = None
    try:
        member = int(proc.stdout.readline())
        run_log.record_script(tmp_path / "workflows-runs" / "qa", "r-cut", pid=proc.pid,
                              started=process_identity(proc.pid)["started"], node_id="build")
        proc.stdin.close()
        proc.wait(timeout=10)         # the leader is gone; its member stays in the group
        reply = await _launch(tmp_path)
        fact = ("processes in the former process group of the script of its step 'build' are "
                f"still running (pid {member}), which may not be that script's")
        assert _still_there_text(fact, resumable=resumable) in reply
        os.kill(member, 0)            # the note did not end it
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        if member is not None:
            try:
                os.kill(member, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
async def test_a_cut_run_that_can_never_be_resumed_and_has_nothing_left_is_not_named(tmp_path):
    _cut(tmp_path, "r-cut", started_at=1.0, status="crashed", node="removed")
    assert "Note:" not in await _launch(tmp_path)


@pytest.mark.asyncio
async def test_at_most_ten_cut_runs_are_looked_at(tmp_path, monkeypatch):
    for i in range(40):
        _cut(tmp_path, f"r{i:02d}", started_at=float(i + 1), status="crashed")
    calls = []
    real = run_log.settle_dead_run
    monkeypatch.setattr(run_log, "settle_dead_run",
                        lambda ws, name, run_id: calls.append(run_id) or real(ws, name, run_id))
    reply = await _launch(tmp_path)
    assert len(calls) == 10 and calls[0] == "r39" and calls[-1] == "r30"
    notes = reply.split("\n\nNote: ")[1:]
    assert [n.split(" ")[1] for n in notes] == ["r39", "r38", "r37"]
    assert reply.endswith("\n\nand 7 more.")


@pytest.mark.asyncio
async def test_finished_runs_do_not_use_up_the_ten(tmp_path):
    _cut(tmp_path, "r-cut", started_at=1.0, status="crashed")
    for i in range(12):
        _cut(tmp_path, f"d{i:02d}", started_at=float(10 + i), status="crashed")
        f = tmp_path / "workflows-runs" / "qa" / f"d{i:02d}.json"
        rec = json.loads(f.read_text(encoding="utf-8"))
        rec.update(status="completed", failed_node=None)
        f.write_text(json.dumps(rec), encoding="utf-8")
    assert _resumable_text("r-cut") in await _launch(tmp_path)


@pytest.mark.asyncio
async def test_the_foreground_result_with_nothing_cut_is_the_plain_result(tmp_path):
    _write_workflow(tmp_path, "qa", {"name": "qa", "start": "build", "nodes": [
        {"id": "build", "kind": "script", "command": "echo ok", "next": None}]})
    out = await _launch(tmp_path, background=False)
    assert out == _format_result(_fresh_result(), output_files=False, human_reachable=True)


@pytest.mark.asyncio
async def test_a_cut_run_an_automation_root_names_is_not_named(tmp_path):
    _cut(tmp_path, "r-cut", started_at=1.0, launch=None, root="automation:nightly",
         status="crashed")
    assert "Note:" not in await _launch(tmp_path, session="automation:nightly")


@pytest.mark.asyncio
async def test_at_most_three_are_named_newest_first(tmp_path):
    for i in range(1, 6):
        _cut(tmp_path, f"r{i}", started_at=float(i), status="crashed")
    reply = await _launch(tmp_path)
    notes = reply.split("\n\nNote: ")[1:]
    assert [n.split(" ")[1] for n in notes[:3]] == ["r5", "r4", "r3"]
    assert len(notes) == 3
    assert "run r2 of" not in reply and "run r1 of" not in reply
    assert reply.endswith("\n\nand 2 more.")


@pytest.mark.asyncio
async def test_a_resume_call_carries_no_notice(tmp_path):
    _cut(tmp_path, "r-cut", started_at=1.0, status="crashed")
    _cut(tmp_path, "r-other", started_at=2.0, status="crashed")
    reply = await _launch(tmp_path, resume_run_id="r-cut")
    assert "Note:" not in reply


@pytest.mark.asyncio
async def test_the_foreground_result_carries_the_notice_too(tmp_path):
    _cut(tmp_path, "r-cut", started_at=1.0, status="crashed")
    out = await _launch(tmp_path, background=False)
    assert out.startswith("Workflow run ")
    assert out.endswith("\n\n" + _resumable_text("r-cut"))


@pytest.mark.asyncio
async def test_an_old_record_without_launch_is_matched_by_its_root(tmp_path):
    _cut(tmp_path, "r-old", started_at=1.0, launch=None, status="crashed")
    assert _resumable_text("r-old") in await _launch(tmp_path)


@pytest.mark.asyncio
async def test_a_launch_with_no_calling_session_gets_no_notice(tmp_path):
    _cut(tmp_path, "r-cut", started_at=1.0, launch=None, root=None, status="crashed")
    assert "Note:" not in await _launch(tmp_path, session=None)


@pytest.mark.asyncio
async def test_a_failure_building_the_notice_does_not_stop_the_launch(tmp_path, monkeypatch):
    _cut(tmp_path, "r-cut", started_at=1.0, status="crashed")

    def boom(*_a, **_kw):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(run_log, "runs_launched_by", boom)
    reply = await _launch(tmp_path)
    assert reply.startswith("Workflow 'qa' started in the background") and "Note:" not in reply


# ── what the agent's resume and refusal leave in telemetry ──

@pytest.mark.asyncio
async def test_the_run_tool_counts_a_claimed_resume(tmp_path, telemetry_dir):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"})
    fake = MagicMock(spec=LLMProvider)
    fake.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake):
        await _tool(tmp_path).execute(name="doer", task="", resume_run_id="r1",
                                      background=False)
    assert payloads(telemetry_dir, "workflow.run_resumed") == [
        {"workflow": "doer", "run_id": "r1", "kind": "dead", "surface": "agent"}]
    assert payloads(telemetry_dir, "workflow.resume_refused") == []
    assert [p["run_id"] for p in payloads(telemetry_dir, "workflow.run_found_dead")] == ["r1"]
    assert_clean(recorded(telemetry_dir), "original task")


@pytest.mark.asyncio
async def test_the_run_tool_counts_a_refusal_by_its_word(tmp_path, telemetry_dir):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo again"},
               root_session_key="automation:nightly")
    with patch("durin.workflow.engine.WorkflowEngine.run"):
        await _tool(tmp_path).execute(name="doer", task="", resume_run_id="r1",
                                      background=False)
    assert payloads(telemetry_dir, "workflow.resume_refused") == [
        {"workflow": "doer", "run_id": "r1", "surface": "agent", "reason": "automation"}]
    assert payloads(telemetry_dir, "workflow.run_resumed") == []
    assert_clean(recorded(telemetry_dir), "nightly", "automation:")


@pytest.mark.asyncio
async def test_the_run_tool_counts_a_lost_claim_as_lost_race(tmp_path, telemetry_dir):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo ok"})
    tool = _tool(tmp_path)

    def _provider_while_another_resume_claims(*_a, **_kw):
        ResumeClaim.take(tmp_path, "doer", "r1",
                         expected=run_log.read_manifest(tmp_path, "doer", "r1"))
        fake = MagicMock(spec=LLMProvider)
        fake.get_default_model.return_value = "m"
        return fake

    with patch("durin.providers.factory.make_provider",
               side_effect=_provider_while_another_resume_claims), \
         patch("durin.workflow.engine.WorkflowEngine.run"):
        await tool.execute(name="doer", task="", resume_run_id="r1", background=False)
    assert [p["reason"] for p in payloads(telemetry_dir, "workflow.resume_refused")] == [
        "lost_race"]
    assert payloads(telemetry_dir, "workflow.run_resumed") == []


@pytest.mark.asyncio
async def test_the_run_tool_counts_an_approval_it_leaves_to_a_person_as_other(
        tmp_path, telemetry_dir):
    _dead_doer(tmp_path, {"kind": "script", "command": "echo ok"})
    f = tmp_path / "workflows-runs" / "doer" / "r1.json"
    rec = json.loads(f.read_text(encoding="utf-8"))
    rec.update(status="needs_input", needs_input_node="only", ask_kind="approval",
               owner=None, active_node=None)
    f.write_text(json.dumps(rec), encoding="utf-8")
    await _tool(tmp_path).execute(name="doer", task="yes", resume_run_id="r1",
                                  background=False)
    assert [p["reason"] for p in payloads(telemetry_dir, "workflow.resume_refused")] == ["other"]
