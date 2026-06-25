"""The engine must keep each agent-backed node's session_key (and its worker_index
or branch_id) in the recorded NodeRun — parallel branches, fan-out workers and
subworkflow nodes used to throw the key away, breaking audit attribution."""

from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.spec import parse_workflow


def test_dynamic_fanout_worker_runs_carry_session_key_and_worker_index(tmp_path):
    def runner(req):
        if req.node.id == "orch":
            return NodeRunResponse(output='["a","b","c"]')
        if req.node.id == "done":
            return NodeRunResponse(output="fin")
        # Distinct session_key per worker so we can assert each is preserved.
        return NodeRunResponse(
            output=f"did {req.task}",
            session_key=f"workflow:r:dev:1:{req.worker_index}",
        )

    wf = parse_workflow({"name": "w", "start": "orch", "max_visits": 3, "nodes": [
        {"id": "orch", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "worker": "dev",
         "list_from": "orch", "max_concurrency": 2, "next": "done"},
        {"id": "dev", "kind": "work"},
        {"id": "done", "kind": "work", "next": None}]})
    res = WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "go")

    worker_runs = sorted(
        (r for r in res.runs if r.node_id == "dev"),
        key=lambda r: r.worker_index,
    )
    assert [r.worker_index for r in worker_runs] == [0, 1, 2]
    assert [r.session_key for r in worker_runs] == [
        "workflow:r:dev:1:0", "workflow:r:dev:1:1", "workflow:r:dev:1:2",
    ]


def test_static_parallel_branch_runs_carry_session_key_and_branch_id(tmp_path):
    def runner(req):
        # Distinct session_key per branch.
        return NodeRunResponse(
            output=f"did {req.node.id}",
            session_key=f"workflow:r:{req.node.id}:1",
        )

    wf = parse_workflow({"name": "w", "start": "fan", "max_visits": 3, "nodes": [
        {"id": "fan", "kind": "parallel", "branches": ["b1", "b2"],
         "reconcile": "read", "next": None},
        {"id": "b1", "kind": "work"},
        {"id": "b2", "kind": "work"}]})
    res = WorkflowEngine(runner, workspace=str(tmp_path)).run(wf, "go")

    branch_runs = {r.branch_id: r for r in res.runs if r.branch_id is not None}
    assert set(branch_runs) == {"b1", "b2"}
    assert branch_runs["b1"].session_key == "workflow:r:b1:1"
    assert branch_runs["b2"].session_key == "workflow:r:b2:1"
