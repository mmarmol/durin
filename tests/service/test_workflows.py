"""Tests for WorkflowsService (list / load / save / delete)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from durin.config.schema import ToolsConfig, WorkflowConfig
from durin.providers.base import LLMProvider
from durin.service.principal import Principal
from durin.service.types import NotFoundError, ValidationFailedError
from durin.service.workflows import (
    WorkflowDeleteCommand,
    WorkflowDuplicateCommand,
    WorkflowGetQuery,
    WorkflowRunCommand,
    WorkflowRunResult,
    WorkflowSaveCommand,
    WorkflowSessionRunsQuery,
    WorkflowsListQuery,
    WorkflowsService,
)
from durin.session.manager import SessionManager
from durin.workflow import run_log
from durin.workflow.result import NodeRun, WorkflowResult

_VALID = {"name": "wf", "start": "a", "nodes": [{"id": "a", "kind": "work"}]}


def _svc(tmp_path):
    return WorkflowsService(workspace=tmp_path)


def _runnable_svc(tmp_path):
    # A service wired for the run route: app_config + sessions, like the gateway builds it.
    app_config = SimpleNamespace(
        resolve_default_preset=lambda: object(),
        tools=ToolsConfig(), workflow=WorkflowConfig(),
    )
    sessions = SessionManager(workspace=tmp_path)
    return WorkflowsService(workspace=tmp_path, app_config=app_config, sessions=sessions)


@pytest.mark.asyncio
async def test_save_list_get_delete_round_trip(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(WorkflowSaveCommand(name="wf", definition=_VALID), p)
    assert (await svc.list(WorkflowsListQuery(), p)).workflows == ["wf"]

    got = await svc.get(WorkflowGetQuery(name="wf"), p)
    assert got.definition["start"] == "a"               # the raw on-disk JSON, round-tripped
    assert (tmp_path / "workflows" / "wf.json").is_file()

    await svc.delete(WorkflowDeleteCommand(name="wf"), p)
    assert (await svc.list(WorkflowsListQuery(), p)).workflows == []


@pytest.mark.asyncio
async def test_save_and_delete_leave_no_lock_inside_versioned_dir(tmp_path):
    # The cross-process lock must live beside the workflows dir, not inside it, or its
    # ".lock" file would land in a version-store snapshot. Lock on the same target the
    # version store uses so an editor write and a snapshot commit never interleave.
    svc, p = _svc(tmp_path), Principal.local()
    wf_dir = tmp_path / "workflows"
    await svc.save(WorkflowSaveCommand(name="wf", definition=_VALID), p)
    await svc.delete(WorkflowDeleteCommand(name="wf"), p)
    assert list(wf_dir.glob("*.lock")) == []
    assert (tmp_path / ".workflow-version.lock").exists()  # lock landed beside the dir


@pytest.mark.asyncio
async def test_save_rejects_an_invalid_workflow(tmp_path):
    with pytest.raises(ValidationFailedError):
        # missing start + nodes -> parse_workflow rejects it, so it never lands on disk
        await _svc(tmp_path).save(WorkflowSaveCommand(name="bad", definition={"name": "bad"}), Principal.local())
    assert not (tmp_path / "workflows" / "bad.json").exists()


@pytest.mark.asyncio
async def test_get_missing_raises_not_found(tmp_path):
    with pytest.raises(NotFoundError):
        await _svc(tmp_path).get(WorkflowGetQuery(name="ghost"), Principal.local())


@pytest.mark.asyncio
async def test_duplicate_copies_under_a_new_name(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(WorkflowSaveCommand(name="wf", definition=_VALID), p)
    res = await svc.duplicate(WorkflowDuplicateCommand(name="wf", target="wf-copy"), p)
    assert res.name == "wf-copy"
    names = (await svc.list(WorkflowsListQuery(), p)).workflows
    assert "wf" in names and "wf-copy" in names
    got = await svc.get(WorkflowGetQuery(name="wf-copy"), p)
    assert got.definition["name"] == "wf-copy"     # inner name updated to the new name
    assert got.definition["start"] == "a"          # the rest of the graph copied verbatim


@pytest.mark.asyncio
async def test_duplicate_does_not_overwrite_an_existing_target(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(WorkflowSaveCommand(name="wf", definition=_VALID), p)
    await svc.save(WorkflowSaveCommand(name="taken", definition={**_VALID, "name": "taken"}), p)
    with pytest.raises(ValidationFailedError):
        await svc.duplicate(WorkflowDuplicateCommand(name="wf", target="taken"), p)
    # the existing target is left untouched
    assert (await svc.get(WorkflowGetQuery(name="taken"), p)).definition["name"] == "taken"


@pytest.mark.asyncio
async def test_duplicate_missing_source_raises_not_found(tmp_path):
    with pytest.raises(NotFoundError):
        await _svc(tmp_path).duplicate(
            WorkflowDuplicateCommand(name="ghost", target="x"), Principal.local()
        )


@pytest.mark.asyncio
async def test_duplicate_rejects_an_empty_target(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    await svc.save(WorkflowSaveCommand(name="wf", definition=_VALID), p)
    with pytest.raises(ValidationFailedError):
        await svc.duplicate(WorkflowDuplicateCommand(name="wf", target="  "), p)


@pytest.mark.asyncio
async def test_delete_missing_raises_not_found(tmp_path):
    with pytest.raises(NotFoundError):
        await _svc(tmp_path).delete(WorkflowDeleteCommand(name="ghost"), Principal.local())


def test_run_command_accepts_input_files():
    """WorkflowRunCommand accepts an optional input_files list (smoke test for the model)."""
    cmd_default = WorkflowRunCommand(name="wf", task="go")
    assert cmd_default.input_files == []

    cmd_with = WorkflowRunCommand(name="wf", task="go", input_files=["/tmp/a.txt", "/tmp/b.txt"])
    assert cmd_with.input_files == ["/tmp/a.txt", "/tmp/b.txt"]


def test_run_command_accepts_an_output_format_override():
    """WorkflowRunCommand accepts an optional call-time output_format (smoke test)."""
    assert WorkflowRunCommand(name="wf", task="go").output_format == ""
    cmd = WorkflowRunCommand(name="wf", task="go", output_format="a bulleted list")
    assert cmd.output_format == "a bulleted list"


def test_workflow_run_result_forwards_exhausted_node():
    """WorkflowRunResult carries exhausted_node from an engine WorkflowResult."""
    engine_result = WorkflowResult(
        status="exhausted",
        final_output="partial output",
        runs=[NodeRun(node_id="loop_node", iteration=5, output="last attempt")],
        exhausted_node="loop_node",
    )
    dto = WorkflowRunResult(
        status=engine_result.status,
        final_output=engine_result.final_output or "",
        run_id=engine_result.run_id,
        runs=[
            {"node_id": r.node_id, "iteration": r.iteration, "passed": r.passed,
             "output": (r.output or "")[:2000]}
            for r in engine_result.runs
        ],
        output_dir=engine_result.output_dir or "",
        exhausted_node=engine_result.exhausted_node or "",
    )
    assert dto.status == "exhausted"
    assert dto.exhausted_node == "loop_node"


def test_workflow_run_result_exhausted_node_defaults_empty():
    """WorkflowRunResult.exhausted_node defaults to empty string for non-exhausted runs."""
    engine_result = WorkflowResult(
        status="completed",
        final_output="done",
        runs=[],
        exhausted_node=None,
    )
    dto = WorkflowRunResult(
        status=engine_result.status,
        final_output=engine_result.final_output or "",
        run_id=engine_result.run_id,
        runs=[],
        output_dir=engine_result.output_dir or "",
        exhausted_node=engine_result.exhausted_node or "",
    )
    assert dto.exhausted_node == ""


@pytest.mark.asyncio
async def test_run_response_carries_needs_input_node_and_output_files(tmp_path):
    svc, p = _runnable_svc(tmp_path), Principal.local()
    await svc.save(WorkflowSaveCommand(name="wf", definition=_VALID), p)
    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "m"

    def fake_run(self, workflow, task, *, root_session_key=None, input_files=None,
                 output_format=None, resume=None):
        return WorkflowResult(
            status="needs_input", run_id="r1", final_output="what env?",
            needs_input_node="a", runs=[], output_files=["a.md"],
        )

    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.workflow.engine.WorkflowEngine.run", fake_run):
        result = await svc.run(WorkflowRunCommand(name="wf", task="go"), p)

    assert result.needs_input_node == "a"
    assert result.output_files == ["a.md"]


@pytest.mark.asyncio
async def test_run_resumes_a_needs_input_manifest_with_the_original_task(tmp_path):
    svc, p = _runnable_svc(tmp_path), Principal.local()
    await svc.save(WorkflowSaveCommand(name="wf", definition=_VALID), p)
    needs_input = WorkflowResult(
        status="needs_input", run_id="r1", final_output="what env?", needs_input_node="a",
        runs=[NodeRun(node_id="a", iteration=1, output="asking")],
    )
    run_log.finalize_run(
        tmp_path, "wf", needs_input,
        root_session_key=None, started_at=1.0, finished_at=2.0, task="original task",
    )
    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "m"
    captured = {}

    def fake_run(self, workflow, task, *, root_session_key=None, input_files=None,
                 output_format=None, resume=None):
        captured["task"] = task
        captured["resume"] = resume
        return WorkflowResult(status="completed", run_id="r1", final_output="ok", runs=[])

    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.workflow.engine.WorkflowEngine.run", fake_run):
        await svc.run(WorkflowRunCommand(name="wf", task="prod env", resume_run_id="r1"), p)

    assert captured["resume"] is not None
    assert captured["resume"].start_at == "a"
    assert captured["task"] == "original task"           # the manifest's original task, not the answers
    assert "prod env" in captured["resume"].upstream      # the answers are threaded as upstream input


@pytest.mark.asyncio
async def test_run_resume_of_a_non_needs_input_run_is_rejected_without_running_the_engine(tmp_path):
    svc, p = _runnable_svc(tmp_path), Principal.local()
    await svc.save(WorkflowSaveCommand(name="wf", definition=_VALID), p)
    completed = WorkflowResult(status="completed", run_id="r1", final_output="done", runs=[])
    run_log.finalize_run(
        tmp_path, "wf", completed,
        root_session_key=None, started_at=1.0, finished_at=2.0, task="original task",
    )
    fake_provider = MagicMock(spec=LLMProvider)
    fake_provider.get_default_model.return_value = "m"
    with patch("durin.providers.factory.make_provider", return_value=fake_provider), \
         patch("durin.workflow.engine.WorkflowEngine.run") as fake_run:
        with pytest.raises(ValidationFailedError):
            await svc.run(WorkflowRunCommand(name="wf", task="prod env", resume_run_id="r1"), p)
    fake_run.assert_not_called()


# --- session_runs route: optional session -> global feed (F8) ---------------


@pytest.mark.asyncio
async def test_session_runs_with_session_is_unchanged(tmp_path):
    """Passing `session` keeps the existing lineage behavior exactly."""
    svc, p = _svc(tmp_path), Principal.local()
    run_log.finalize_run(tmp_path, "wf", WorkflowResult(
        status="completed", final_output="a", runs=[], run_id="r1"),
        root_session_key="sess:1", started_at=1.0, finished_at=2.0)
    run_log.finalize_run(tmp_path, "wf", WorkflowResult(
        status="completed", final_output="b", runs=[], run_id="r2"),
        root_session_key="sess:2", started_at=3.0, finished_at=4.0)
    result = await svc.session_runs(WorkflowSessionRunsQuery(session="sess:1"), p)
    assert [r["run_id"] for r in result.runs] == ["r1"]


@pytest.mark.asyncio
async def test_session_runs_without_session_returns_global_feed(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    run_log.finalize_run(tmp_path, "alpha", WorkflowResult(
        status="completed", final_output="a", runs=[], run_id="r1"),
        root_session_key="sess:1", started_at=1.0, finished_at=2.0)
    run_log.finalize_run(tmp_path, "beta", WorkflowResult(
        status="completed", final_output="b", runs=[], run_id="r2"),
        root_session_key="sess:2", started_at=3.0, finished_at=4.0)
    result = await svc.session_runs(WorkflowSessionRunsQuery(), p)
    assert {r["run_id"] for r in result.runs} == {"r1", "r2"}
    assert {r["workflow"] for r in result.runs} == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_session_runs_without_session_respects_limit(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    for i in range(3):
        run_log.finalize_run(tmp_path, "wf", WorkflowResult(
            status="completed", final_output="x", runs=[], run_id=f"r{i}"),
            root_session_key=None, started_at=float(i), finished_at=float(i))
    result = await svc.session_runs(WorkflowSessionRunsQuery(limit=2), p)
    assert len(result.runs) == 2
    assert [r["run_id"] for r in result.runs] == ["r2", "r1"]


@pytest.mark.asyncio
async def test_session_runs_global_feed_carries_questions_on_needs_input(tmp_path):
    svc, p = _svc(tmp_path), Principal.local()
    run_log.finalize_run(tmp_path, "wf", WorkflowResult(
        status="needs_input", final_output="which env?", runs=[], run_id="r1",
        needs_input_node="gate"), root_session_key=None, started_at=1.0, finished_at=1.0)
    result = await svc.session_runs(WorkflowSessionRunsQuery(), p)
    assert result.runs[0]["questions"] == "which env?"
