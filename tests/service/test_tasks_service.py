"""Tests for TasksService node-tree enrichment on workflow tasks."""

import pytest

from durin.service.principal import Principal
from durin.service.tasks import TasksListQuery, TasksService
from durin.workflow import run_log
from durin.workflow.result import NodeRun, WorkflowResult


def _principal():
    return Principal.local()


@pytest.fixture()
def tmp_workspace_with_manifest(tmp_path):
    """Workspace with one completed workflow run whose nodes are plan, search, search, gather."""
    result = WorkflowResult(
        status="completed",
        final_output="done",
        run_id="r1",
        runs=[
            NodeRun(node_id="plan", iteration=0, output="planned", session_key="sk-plan", status="ok"),
            NodeRun(node_id="search", iteration=0, output="found1", session_key="sk-s1", status="ok"),
            NodeRun(node_id="search", iteration=1, output="found2", session_key="sk-s2", status="ok"),
            NodeRun(node_id="gather", iteration=0, output="gathered", session_key="sk-gather", status="ok"),
        ],
    )
    run_log.finalize_run(
        tmp_path, "my-wf", result,
        root_session_key="websocket:c1", started_at=1.0, finished_at=2.0,
    )
    return tmp_path


@pytest.mark.asyncio
async def test_workflow_task_carries_node_tree(tmp_workspace_with_manifest):
    svc = TasksService(workspace=tmp_workspace_with_manifest)
    res = await svc.list(TasksListQuery(session="websocket:c1"), _principal())
    wf = [t for t in res.tasks if t.kind == "workflow"][0]
    assert wf.nodes is not None
    assert [n["id"] for n in wf.nodes][:1] == ["plan"]
    assert all("status" in n for n in wf.nodes)


@pytest.mark.asyncio
async def test_node_tree_collapses_repeated_node_ids(tmp_workspace_with_manifest):
    """A node id that appears in multiple iterations collapses to one entry (latest status)."""
    svc = TasksService(workspace=tmp_workspace_with_manifest)
    res = await svc.list(TasksListQuery(session="websocket:c1"), _principal())
    wf = [t for t in res.tasks if t.kind == "workflow"][0]
    ids = [n["id"] for n in wf.nodes]
    # search appears twice in iterations but collapses to one entry
    assert ids.count("search") == 1
    # order: plan, search, gather (first-seen)
    assert ids == ["plan", "search", "gather"]


@pytest.mark.asyncio
async def test_subagent_task_has_no_node_tree(tmp_path):
    """Sub-agent tasks leave nodes as None."""
    class _Status:
        task_id = "t1"; label = "research"; phase = "done"; session_key = "subagent:t1"
        started_at = 0.0; ended_at = 1.0

    class _Mgr:
        def list_for_session(self, s):
            return [_Status()]

    svc = TasksService(workspace=tmp_path, subagent_manager=_Mgr())
    res = await svc.list(TasksListQuery(session="websocket:c1"), _principal())
    sub = [t for t in res.tasks if t.kind == "subagent"]
    assert len(sub) == 1
    assert sub[0].nodes is None


@pytest.mark.asyncio
async def test_node_failed_status_maps_to_failed(tmp_path):
    """node_failed and persist_failed statuses map to 'failed' in the node tree."""
    result = WorkflowResult(
        status="completed",
        final_output="partial",
        run_id="r2",
        runs=[
            NodeRun(node_id="plan", iteration=0, output="ok", session_key="sk1", status="ok"),
            NodeRun(node_id="work", iteration=0, output="err", session_key="sk2", status="node_failed"),
        ],
    )
    run_log.finalize_run(
        tmp_path, "wf2", result,
        root_session_key="websocket:c2", started_at=1.0, finished_at=2.0,
    )
    svc = TasksService(workspace=tmp_path)
    res = await svc.list(TasksListQuery(session="websocket:c2"), _principal())
    wf = [t for t in res.tasks if t.kind == "workflow"][0]
    node_map = {n["id"]: n for n in wf.nodes}
    assert node_map["plan"]["status"] == "done"
    assert node_map["work"]["status"] == "failed"
