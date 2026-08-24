"""Renaming or deleting a workflow must not break the automations that run it.

`rename` repoints `workflow` in every automation that names the renamed
workflow (`WorkflowsService._repoint_automations`), and `delete` refuses while
an automation still depends on it, via `durin.registry_graph.dependents_of`.
"""

import pytest

from durin.automations.spec import parse_automation
from durin.automations.store import automations_dir, load_automation, save_automation
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


def _automation(tmp_path, name, workflow):
    save_automation(tmp_path, parse_automation({"name": name, "workflow": workflow}))


async def _save(svc, name, definition=None):
    await svc.save(WorkflowSaveCommand(name=name, definition=definition or _VALID),
                   Principal.local())


@pytest.mark.asyncio
async def test_rename_repoints_the_automations_that_run_it(tmp_path):
    svc = _svc(tmp_path)
    await _save(svc, "stage1")
    _automation(tmp_path, "nightly", "stage1")

    await svc.rename(WorkflowRenameCommand(name="stage1", target="context"), Principal.local())

    assert load_automation(tmp_path, "nightly").workflow == "context"


@pytest.mark.asyncio
async def test_the_repointed_automation_is_versioned_too(tmp_path):
    """The automation lives in its own store; its edit needs its own commit or
    the change is invisible to automation history."""
    from durin.automations.version_store import AutomationVersionStore

    svc = _svc(tmp_path)
    await _save(svc, "stage1")
    _automation(tmp_path, "nightly", "stage1")
    before = len(AutomationVersionStore(automations_dir(tmp_path)).history("nightly"))

    await svc.rename(WorkflowRenameCommand(name="stage1", target="context"), Principal.local())

    assert len(AutomationVersionStore(automations_dir(tmp_path)).history("nightly")) > before


@pytest.mark.asyncio
async def test_rename_leaves_unrelated_automations_alone(tmp_path):
    svc = _svc(tmp_path)
    await _save(svc, "stage1")
    await _save(svc, "other")
    _automation(tmp_path, "nightly", "other")

    await svc.rename(WorkflowRenameCommand(name="stage1", target="context"), Principal.local())

    assert load_automation(tmp_path, "nightly").workflow == "other"


@pytest.mark.asyncio
async def test_delete_refuses_a_workflow_an_automation_runs(tmp_path):
    svc = _svc(tmp_path)
    await _save(svc, "stage1")
    _automation(tmp_path, "nightly", "stage1")

    with pytest.raises(ValidationFailedError) as exc:
        await svc.delete(WorkflowDeleteCommand(name="stage1"), Principal.local())

    assert "nightly" in str(exc.value)
    assert (tmp_path / "workflows" / "stage1.json").is_file()


@pytest.mark.asyncio
async def test_delete_still_works_when_nothing_depends_on_it(tmp_path):
    svc = _svc(tmp_path)
    await _save(svc, "lonely")

    await svc.delete(WorkflowDeleteCommand(name="lonely"), Principal.local())

    assert not (tmp_path / "workflows" / "lonely.json").exists()
