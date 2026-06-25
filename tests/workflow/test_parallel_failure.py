"""Per-worker / per-branch failure isolation: a single unit raising in a parallel
node records a ``node_failed`` NodeRun for that unit and lets the others complete; the
merged output notes the failures. Only when EVERY unit fails does the node abort the run."""

from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.spec import parse_workflow


def _fanout_wf():
    return parse_workflow({"name": "w", "start": "orch", "max_visits": 3, "nodes": [
        {"id": "orch", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "worker": "dev",
         "list_from": "orch", "max_concurrency": 4, "next": "done"},
        {"id": "dev", "kind": "work"},
        {"id": "done", "kind": "work", "next": None}]})


def test_one_worker_failure_is_isolated(tmp_path):
    # Worker index 2 raises; the other three complete and the run is NOT aborted.
    def runner(req):
        if req.node.id == "orch":
            return NodeRunResponse(output='["a","b","c","d"]')
        if req.node.id == "done":
            return NodeRunResponse(output="fin")
        if req.worker_index == 2:
            raise RuntimeError("worker 2 boom")
        return NodeRunResponse(output=f"did {req.task}",
                               session_key=f"workflow:r1:dev:1:{req.worker_index}")

    res = WorkflowEngine(runner, workspace=str(tmp_path),
                         run_id_factory=lambda: "r1").run(_fanout_wf(), "go")
    assert res.status == "completed"
    worker_runs = {r.worker_index: r for r in res.runs if r.node_id == "dev"}
    assert worker_runs[0].status == "ok"
    assert worker_runs[1].status == "ok"
    assert worker_runs[3].status == "ok"
    assert worker_runs[2].status == "node_failed"
    assert worker_runs[2].error
    # The merged output of the fan node notes the failed worker.
    merged = next(r.output for r in res.runs if r.node_id == "fan")
    assert "2" in merged


def test_all_workers_failing_aborts_and_names_node(tmp_path):
    def runner(req):
        if req.node.id == "orch":
            return NodeRunResponse(output='["a","b"]')
        if req.node.id == "done":
            return NodeRunResponse(output="fin")
        raise RuntimeError("everything is broken")

    res = WorkflowEngine(runner, workspace=str(tmp_path),
                         run_id_factory=lambda: "r1").run(_fanout_wf(), "go")
    assert res.status == "aborted"
    assert "fan" in (res.final_output or "")
    # Both workers recorded as failed.
    failed = [r for r in res.runs if r.node_id == "dev" and r.status == "node_failed"]
    assert len(failed) == 2


def _read_parallel_wf():
    return parse_workflow({"name": "w", "start": "p", "max_visits": 3, "nodes": [
        {"id": "p", "kind": "parallel", "reconcile": "read",
         "branches": ["x", "y"], "max_concurrency": 2, "next": None},
        {"id": "x", "kind": "work"},
        {"id": "y", "kind": "work"}]})


def test_one_read_branch_failure_is_isolated(tmp_path):
    def runner(req):
        if req.node.id == "y":
            raise RuntimeError("branch y boom")
        return NodeRunResponse(output=f"out {req.node.id}",
                               session_key=f"workflow:r1:{req.node.id}:1")

    res = WorkflowEngine(runner, workspace=str(tmp_path),
                         run_id_factory=lambda: "r1").run(_read_parallel_wf(), "go")
    assert res.status == "completed"
    by_branch = {r.branch_id: r for r in res.runs if r.branch_id is not None}
    assert by_branch["x"].status == "ok"
    assert by_branch["y"].status == "node_failed"
    assert by_branch["y"].error


def test_all_read_branches_failing_aborts(tmp_path):
    def runner(req):
        raise RuntimeError("both branches broken")

    res = WorkflowEngine(runner, workspace=str(tmp_path),
                         run_id_factory=lambda: "r1").run(_read_parallel_wf(), "go")
    assert res.status == "aborted"
    assert "p" in (res.final_output or "")
