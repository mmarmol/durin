"""Renaming or deleting a workflow must not break what runs it.

`rename` already repoints `subworkflow` nodes in sibling definitions, but loops
live in a different directory and were never touched — so renaming a workflow a
loop runs left the loop pointing at a name that no longer resolves. `delete`
checked nothing at all.
"""

import json

import pytest

from durin.loops.store import load_loop, loops_dir, save_loop
from durin.loops.spec import parse_loop
from durin.service.principal import Principal
from durin.service.types import ValidationFailedError
from durin.service.workflows import (
    WorkflowDeleteCommand,
    WorkflowRenameCommand,
    WorkflowSaveCommand,
    WorkflowsService,
)

_VALID = {"name": "wf", "start": "a", "nodes": [{"id": "a", "kind": "work"}]}


def _svc(tmp_path):
    return WorkflowsService(workspace=tmp_path)


def _loop(tmp_path, name, workflow):
    save_loop(tmp_path, parse_loop(
        {"name": name, "workflow": workflow, "goal": {"intent": "x"}}),
        actor="user", reason="test fixture")


async def _save(svc, name, definition=None):
    await svc.save(WorkflowSaveCommand(name=name, definition=definition or _VALID),
                   Principal.local())


@pytest.mark.asyncio
async def test_rename_repoints_the_loops_that_run_it(tmp_path):
    svc = _svc(tmp_path)
    await _save(svc, "stage1")
    _loop(tmp_path, "nightly", "stage1")

    await svc.rename(WorkflowRenameCommand(name="stage1", target="context"), Principal.local())

    assert load_loop(tmp_path, "nightly").workflow == "context"


@pytest.mark.asyncio
async def test_the_repointed_loop_is_versioned_too(tmp_path):
    """The loop lives in its own store; its edit needs its own commit or the
    change is invisible to loop history."""
    from durin.loops.version_store import LoopVersionStore

    svc = _svc(tmp_path)
    await _save(svc, "stage1")
    _loop(tmp_path, "nightly", "stage1")
    before = len(LoopVersionStore(loops_dir(tmp_path)).history("nightly"))

    await svc.rename(WorkflowRenameCommand(name="stage1", target="context"), Principal.local())

    assert len(LoopVersionStore(loops_dir(tmp_path)).history("nightly")) > before


@pytest.mark.asyncio
async def test_rename_leaves_unrelated_loops_alone(tmp_path):
    svc = _svc(tmp_path)
    await _save(svc, "stage1")
    await _save(svc, "other")
    _loop(tmp_path, "nightly", "other")

    await svc.rename(WorkflowRenameCommand(name="stage1", target="context"), Principal.local())

    assert load_loop(tmp_path, "nightly").workflow == "other"


@pytest.mark.asyncio
async def test_delete_refuses_a_workflow_a_loop_runs(tmp_path):
    svc = _svc(tmp_path)
    await _save(svc, "stage1")
    _loop(tmp_path, "nightly", "stage1")

    with pytest.raises(ValidationFailedError) as exc:
        await svc.delete(WorkflowDeleteCommand(name="stage1"), Principal.local())

    assert "nightly" in str(exc.value)
    assert (tmp_path / "workflows" / "stage1.json").is_file()


@pytest.mark.asyncio
async def test_delete_refuses_a_workflow_another_calls_as_a_subflow(tmp_path):
    svc = _svc(tmp_path)
    await _save(svc, "child")
    await _save(svc, "parent", {
        "name": "parent", "start": "s",
        "nodes": [{"id": "s", "kind": "subworkflow", "workflow": "child"}]})

    with pytest.raises(ValidationFailedError) as exc:
        await svc.delete(WorkflowDeleteCommand(name="child"), Principal.local())

    assert "parent" in str(exc.value)


@pytest.mark.asyncio
async def test_delete_still_works_when_nothing_depends_on_it(tmp_path):
    svc = _svc(tmp_path)
    await _save(svc, "lonely")

    await svc.delete(WorkflowDeleteCommand(name="lonely"), Principal.local())

    assert not (tmp_path / "workflows" / "lonely.json").exists()
