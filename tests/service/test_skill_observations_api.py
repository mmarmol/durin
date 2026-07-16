"""SkillsService skill-observation endpoints.

Tests the list/resolve surface:
  GET  /api/v1/skills/observations
  POST /api/v1/skills/observations/{id}/resolve

Uses the same direct-construction pattern as test_skill_suggestions_api.py:
  SkillsService(workspace=...) + Principal.local()
"""

from __future__ import annotations

import pytest

from durin.agent import skill_observations as so
from durin.service.principal import Principal
from durin.service.skills import (
    ResolveObservationCommand,
    SkillObservationsQuery,
    SkillsService,
)
from durin.service.types import NotFoundError, ValidationFailedError


def _log(ws, **kw):
    base = {"skill": "deploy-gateway", "kind": "correction",
            "issue": "user corrected the wheel build step",
            "improvement": "build from local dist, not PyPI"}
    base.update(kw)
    return so.log_observation(ws, **base)


@pytest.mark.asyncio
async def test_list_returns_open_observations(tmp_path):
    ws = tmp_path
    _log(ws, issue="a")
    _log(ws, skill="doc-sync", kind="gap", issue="b")

    svc = SkillsService(workspace=ws)
    listed = await svc.observations(SkillObservationsQuery(), Principal.local())
    assert [o.id for o in listed.observations] == [1, 2]
    first = listed.observations[0]
    assert first.skill == "deploy-gateway"
    assert first.kind == "correction"
    assert first.issue == "a"
    assert first.improvement == "build from local dist, not PyPI"
    assert first.count == 1
    assert first.first_seen and first.last_seen


@pytest.mark.asyncio
async def test_list_filters_by_skill(tmp_path):
    ws = tmp_path
    _log(ws, issue="a")
    _log(ws, skill="doc-sync", issue="b")

    svc = SkillsService(workspace=ws)
    listed = await svc.observations(
        SkillObservationsQuery(skill="doc-sync"), Principal.local())
    assert [o.skill for o in listed.observations] == ["doc-sync"]


@pytest.mark.asyncio
async def test_list_empty_without_store(tmp_path):
    svc = SkillsService(workspace=tmp_path)
    listed = await svc.observations(SkillObservationsQuery(), Principal.local())
    assert listed.observations == []


@pytest.mark.asyncio
async def test_resolve_applied_and_declined(tmp_path):
    ws = tmp_path
    _log(ws, issue="a")
    _log(ws, issue="b")

    svc = SkillsService(workspace=ws)
    pr = Principal.local()
    await svc.resolve_observation(
        ResolveObservationCommand(id=1, disposition="applied"), pr)
    await svc.resolve_observation(
        ResolveObservationCommand(id=2, disposition="declined"), pr)
    assert so.open_observations(ws) == []
    assert [r["id"] for r in so.declined_observations(ws)] == [2]


@pytest.mark.asyncio
async def test_resolve_unknown_id_raises_not_found(tmp_path):
    svc = SkillsService(workspace=tmp_path)
    with pytest.raises(NotFoundError):
        await svc.resolve_observation(
            ResolveObservationCommand(id=99, disposition="applied"),
            Principal.local())


@pytest.mark.asyncio
async def test_resolve_bad_disposition_raises_validation(tmp_path):
    ws = tmp_path
    _log(ws)
    svc = SkillsService(workspace=ws)
    with pytest.raises(ValidationFailedError):
        await svc.resolve_observation(
            ResolveObservationCommand(id=1, disposition="keep"),
            Principal.local())
    assert len(so.open_observations(ws)) == 1
