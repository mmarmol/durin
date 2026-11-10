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

    def fake_run(self, workflow, task, *, root_session_key=None, input_files=None, output_format=None, resume=None, work_key=None):
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

    def fake_run(self, workflow, task, *, root_session_key=None, input_files=None, output_format=None, resume=None, work_key=None):
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

    def fake_run(self, workflow, task, *, root_session_key=None, input_files=None, output_format=None, resume=None, work_key=None):
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
                 output_format=None, resume=None, work_key=None):
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
