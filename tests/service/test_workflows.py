"""Tests for WorkflowsService (list / load / save / delete)."""

import pytest

from durin.service.principal import Principal
from durin.service.types import NotFoundError, ValidationFailedError
from durin.service.workflows import (
    WorkflowDeleteCommand,
    WorkflowDuplicateCommand,
    WorkflowGetQuery,
    WorkflowRunCommand,
    WorkflowRunResult,
    WorkflowSaveCommand,
    WorkflowsListQuery,
    WorkflowsService,
)
from durin.workflow.result import NodeRun, WorkflowResult

_VALID = {"name": "wf", "start": "a", "nodes": [{"id": "a", "kind": "work"}]}


def _svc(tmp_path):
    return WorkflowsService(workspace=tmp_path)


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
