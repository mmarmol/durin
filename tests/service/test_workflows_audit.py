"""Tests for the workflow auditability surface (E1): the run response carries
``run_id`` + per-node attribution, and the two manifest read routes return a run
manifest / a session's runs."""

import pytest

from durin.service.principal import Principal
from durin.service.types import NotFoundError
from durin.service.workflows import (
    WorkflowRunManifestQuery,
    WorkflowRunResult,
    WorkflowSessionRunsQuery,
    WorkflowsService,
)
from durin.workflow import run_log
from durin.workflow.result import NodeRun, WorkflowResult


def _svc(tmp_path):
    return WorkflowsService(workspace=tmp_path)


def test_run_result_carries_run_id_and_per_node_attribution():
    """The run DTO forwards run_id and each node's session_key/worker_index/status/route_label."""
    engine_result = WorkflowResult(
        status="completed",
        final_output="done",
        run_id="abc123",
        runs=[
            NodeRun(node_id="route", iteration=0, output="picked b",
                    route_label="b", session_key="sess-route", status="ok"),
            NodeRun(node_id="work", iteration=0, output="ran",
                    session_key="sess-work", worker_index=2, status="ok"),
        ],
    )
    dto = WorkflowRunResult(
        status=engine_result.status,
        final_output=engine_result.final_output or "",
        run_id=engine_result.run_id,
        runs=[
            {"node_id": r.node_id, "iteration": r.iteration, "passed": r.passed,
             "session_key": r.session_key, "worker_index": r.worker_index,
             "status": r.status, "route_label": r.route_label,
             "output": (r.output or "")[:2000]}
            for r in engine_result.runs
        ],
        output_dir=engine_result.output_dir or "",
        exhausted_node=engine_result.exhausted_node or "",
    )
    assert dto.run_id == "abc123"
    assert dto.runs[0]["session_key"] == "sess-route"
    assert dto.runs[0]["route_label"] == "b"
    assert dto.runs[1]["session_key"] == "sess-work"
    assert dto.runs[1]["worker_index"] == 2
    assert dto.runs[1]["status"] == "ok"


@pytest.mark.asyncio
async def test_run_manifest_route_returns_the_run(tmp_path):
    """GET .../runs/{run_id} returns the manifest with its per-node trace."""
    result = WorkflowResult(
        status="completed", final_output="done", run_id="r1",
        runs=[NodeRun(node_id="a", iteration=0, output="x", session_key="sk-a")],
    )
    run_log.finalize_run(
        tmp_path, "wf", result,
        root_session_key="root-1", started_at=1.0, finished_at=2.0,
    )
    svc, p = _svc(tmp_path), Principal.local()
    got = await svc.run_manifest(WorkflowRunManifestQuery(name="wf", run_id="r1"), p)
    assert got.manifest["run_id"] == "r1"
    assert got.manifest["status"] == "completed"
    assert got.manifest["runs"][0]["session_key"] == "sk-a"


@pytest.mark.asyncio
async def test_run_manifest_route_missing_raises_not_found(tmp_path):
    with pytest.raises(NotFoundError):
        await _svc(tmp_path).run_manifest(
            WorkflowRunManifestQuery(name="wf", run_id="ghost"), Principal.local()
        )


@pytest.mark.asyncio
async def test_session_runs_route_lists_the_session_runs(tmp_path):
    """GET /workflows/runs?session=<key> lists every run rooted at that session."""
    result = WorkflowResult(
        status="completed", final_output="done", run_id="r1",
        runs=[NodeRun(node_id="a", iteration=0, output="x", session_key="sk-a")],
    )
    run_log.finalize_run(
        tmp_path, "wf", result,
        root_session_key="root-1", started_at=1.0, finished_at=2.0,
    )
    svc, p = _svc(tmp_path), Principal.local()
    got = await svc.session_runs(WorkflowSessionRunsQuery(session="root-1"), p)
    assert [r["run_id"] for r in got.runs] == ["r1"]
    # A session with no runs yields an empty list (not an error).
    none = await svc.session_runs(WorkflowSessionRunsQuery(session="root-other"), p)
    assert none.runs == []
