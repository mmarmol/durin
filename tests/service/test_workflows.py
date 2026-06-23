"""Tests for WorkflowsService (list / load / save / delete)."""

import pytest

from durin.service.principal import Principal
from durin.service.types import NotFoundError, ValidationFailedError
from durin.service.workflows import (
    WorkflowDeleteCommand,
    WorkflowGetQuery,
    WorkflowSaveCommand,
    WorkflowsListQuery,
    WorkflowsService,
)

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
async def test_delete_missing_raises_not_found(tmp_path):
    with pytest.raises(NotFoundError):
        await _svc(tmp_path).delete(WorkflowDeleteCommand(name="ghost"), Principal.local())
