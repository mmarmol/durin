"""A sub-workflow step's run is resumed through the run that called it, never on
its own. Resumed alone it would run its step in its own folder instead of the
caller's, report itself completed and leave the caller stopped."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from durin.config.schema import ToolsConfig, WorkflowConfig
from durin.providers.base import LLMProvider
from durin.service.principal import Principal
from durin.service.types import ValidationFailedError
from durin.service.workflows import WorkflowRunCommand, WorkflowsService
from durin.session.manager import SessionManager
from durin.workflow import run_log
from durin.workflow.engine import (
    NodeExecutionError,
    NodeRunResponse,
    WorkflowEngine,
    resume_refusal,
)
from durin.workflow.loader import workflows_dir
from durin.workflow.spec import parse_workflow
from durin.workflow.subworkflow import SubworkflowRunner


def _write_workflow(workspace, name, data):
    d = workflows_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def _failing_child_runner(req):
    if req.node.id == "inner":
        raise NodeExecutionError(req.node.id, req.iteration, None, RuntimeError("boom"))
    return NodeRunResponse(output=f"{req.node.id}-out")


def test_a_failed_child_run_is_refused_and_its_caller_is_not(tmp_path):
    _write_workflow(tmp_path, "child", {"name": "child", "start": "inner",
                                        "nodes": [{"id": "inner", "kind": "work", "next": None}]})
    parent = parse_workflow({"name": "caller", "start": "call", "nodes": [
        {"id": "call", "kind": "subworkflow", "workflow": "child", "next": None}]})
    engine = WorkflowEngine(
        node_runner=_failing_child_runner, run_id_factory=lambda: "caller1",
        workspace=str(tmp_path),
        subworkflow_runner=SubworkflowRunner(tmp_path, _failing_child_runner, judge_runner=None))

    assert engine.run(parent, "t").status == "aborted"

    caller = run_log.read_manifest(tmp_path, "caller", "caller1")
    [child_path] = (tmp_path / "workflows-runs" / "child").glob("*.json")
    child = json.loads(child_path.read_text(encoding="utf-8"))
    assert child["status"] == "aborted" and child["failed_node"] == "inner"
    assert child["parent_run_id"] == "caller1"
    refusal = resume_refusal(child)
    assert refusal and "caller1" in refusal
    assert resume_refusal(caller) is None


@pytest.mark.parametrize("status,extra", [
    ("needs_input", {"needs_input_node": "inner", "ask_kind": "question"}),
    ("aborted", {"failed_node": "inner"}),
])
def test_a_child_run_is_refused_whether_paused_or_failed(status, extra):
    record = {"run_id": "c1", "status": status, "parent_run_id": "caller1",
              "runs": [{"node_id": "inner", "iteration": 1, "status": "node_failed"}], **extra}
    refusal = resume_refusal(record)
    assert refusal and "caller1" in refusal


def _child_record(tmp_path, status, extra) -> Path:
    _write_workflow(tmp_path, "child", {"name": "child", "start": "inner",
                                        "nodes": [{"id": "inner", "kind": "work", "next": None}]})
    run_log.start_run(tmp_path, "child", "c1", root_session_key="s", started_at=1.0,
                      parent_run_id="caller1")
    path = tmp_path / "workflows-runs" / "child" / "c1.json"
    rec = json.loads(path.read_text(encoding="utf-8"))
    rec.update(status=status, task="the child's task", resume_upstream="in",
               runs=[{"node_id": "inner", "iteration": 1, "status": "node_failed"}], **extra)
    path.write_text(json.dumps(rec), encoding="utf-8")
    return path


_CHILD_STATES = [
    ("needs_input", {"needs_input_node": "inner", "ask_kind": "question",
                     "final_output": "which env?"}),
    ("aborted", {"failed_node": "inner"}),
]


def _provider():
    fake = MagicMock(spec=LLMProvider)
    fake.get_default_model.return_value = "m"
    return patch("durin.providers.factory.make_provider", return_value=fake)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,extra", _CHILD_STATES)
async def test_the_service_refuses_a_child_run_and_leaves_its_record_alone(tmp_path, status, extra):
    path = _child_record(tmp_path, status, extra)
    before = path.read_bytes()
    app_config = SimpleNamespace(resolve_default_preset=lambda: object(),
                                 tools=ToolsConfig(), workflow=WorkflowConfig())
    svc = WorkflowsService(workspace=tmp_path, app_config=app_config,
                           sessions=SessionManager(workspace=tmp_path))
    with _provider(), patch("durin.workflow.engine.WorkflowEngine.run") as fake_run, \
            pytest.raises(ValidationFailedError) as err:
        await svc.run(WorkflowRunCommand(name="child", task="prod", resume_run_id="c1"),
                      Principal.local())
    assert "caller1" in str(err.value) and "cannot be resumed" in str(err.value)
    fake_run.assert_not_called()
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status,extra", _CHILD_STATES)
async def test_the_run_tool_refuses_a_child_run_and_leaves_its_record_alone(tmp_path, status, extra):
    from durin.agent.tools.run_workflow import RunWorkflowTool

    path = _child_record(tmp_path, status, extra)
    before = path.read_bytes()
    app_config = SimpleNamespace(resolve_default_preset=lambda: object(),
                                 tools=ToolsConfig(), workflow=WorkflowConfig())
    tool = RunWorkflowTool.create(SimpleNamespace(
        workspace=str(tmp_path), sessions=SessionManager(workspace=tmp_path),
        app_config=app_config))
    with _provider(), patch("durin.workflow.engine.WorkflowEngine.run") as fake_run:
        out = await tool.execute(name="child", task="prod", resume_run_id="c1", background=False)
    assert out.startswith("Error:") and "caller1" in out and "cannot be resumed" in out
    fake_run.assert_not_called()
    assert path.read_bytes() == before
