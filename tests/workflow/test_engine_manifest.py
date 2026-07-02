"""The engine owns the run manifest: it writes a ``running`` record before the walk,
updates it after each node, and finalizes it at the end — so a run is durable, observable
in-flight, and forward-referenceable from its calling session."""

from durin.workflow import run_log
from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.spec import parse_workflow


def _two_node_wf():
    return parse_workflow({"name": "w", "start": "a", "max_visits": 3, "nodes": [
        {"id": "a", "kind": "work", "next": "b"},
        {"id": "b", "kind": "work", "next": None}]})


def test_manifest_is_finalized_with_node_session_keys(tmp_path):
    def runner(req):
        return NodeRunResponse(output=f"out {req.node.id}",
                               session_key=f"workflow:r1:{req.node.id}:1")

    engine = WorkflowEngine(runner, workspace=str(tmp_path),
                            run_id_factory=lambda: "r1")
    res = engine.run(_two_node_wf(), "go", root_session_key="sess:1")
    assert res.status == "completed"

    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert rec is not None
    assert rec["status"] == "completed"
    assert "finished_at" in rec
    assert rec["root_session_key"] == "sess:1"
    by_node = {r["node_id"]: r for r in rec["runs"]}
    assert by_node["a"]["session_key"] == "workflow:r1:a:1"
    assert by_node["b"]["session_key"] == "workflow:r1:b:1"


def test_manifest_is_running_mid_walk_with_partial_runs(tmp_path):
    seen = {}

    def runner(req):
        # When the second node runs, the first node must already be recorded in a
        # still-"running" manifest on disk — proving update_run fired mid-walk.
        if req.node.id == "b":
            mid = run_log.read_manifest(tmp_path, "w", "r1")
            seen["status"] = mid["status"]
            seen["nodes"] = [r["node_id"] for r in mid["runs"]]
        return NodeRunResponse(output=f"out {req.node.id}",
                               session_key=f"workflow:r1:{req.node.id}:1")

    WorkflowEngine(runner, workspace=str(tmp_path),
                   run_id_factory=lambda: "r1").run(_two_node_wf(), "go")
    assert seen["status"] == "running"
    assert seen["nodes"] == ["a"]   # only the first node, before b completes


def test_headless_run_manifest_uses_effective_root(tmp_path):
    # A headless run (root_session_key=None) roots node sessions under
    # workflow:<run_id>:root; the manifest must record that SAME effective root so
    # runs_for_session(effective_root) finds the run.
    def runner(req):
        return NodeRunResponse(output="x", session_key=f"workflow:r1:{req.node.id}:1")

    WorkflowEngine(runner, workspace=str(tmp_path),
                   run_id_factory=lambda: "r1").run(_two_node_wf(), "go")
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert rec["root_session_key"] == "workflow:r1:root"
    assert [r["run_id"] for r in run_log.runs_for_session(tmp_path, "workflow:r1:root")] == ["r1"]


def test_aborted_run_is_finalized(tmp_path):
    def runner(req):
        raise RuntimeError("boom")

    res = WorkflowEngine(runner, workspace=str(tmp_path),
                         run_id_factory=lambda: "r1").run(_two_node_wf(), "go")
    assert res.status == "aborted"
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert rec["status"] == "aborted"
    assert "finished_at" in rec


def test_config_error_finalizes_manifest_not_left_running(tmp_path):
    # A config/wiring error (a subworkflow node but no subworkflow_runner) is re-raised,
    # but the manifest must be finalized 'aborted' — never left a stale 'running' record
    # that the crash sweep would later mislabel 'crashed'.
    import pytest

    from durin.workflow.engine import WorkflowConfigError
    wf = parse_workflow({"name": "w", "start": "s", "nodes": [
        {"id": "s", "kind": "subworkflow", "workflow": "child", "next": None}]})

    engine = WorkflowEngine(lambda req: NodeRunResponse(output="x"),
                            workspace=str(tmp_path), run_id_factory=lambda: "r1")
    with pytest.raises(WorkflowConfigError):
        engine.run(wf, "go")
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert rec["status"] == "aborted"
    assert "finished_at" in rec


def test_no_manifest_without_workspace(tmp_path):
    # A read-only engine (no workspace) writes no manifest and still runs.
    def runner(req):
        return NodeRunResponse(output="x")

    res = WorkflowEngine(runner, run_id_factory=lambda: "r1").run(_two_node_wf(), "go")
    assert res.status == "completed"
    assert not (tmp_path / "workflows-runs").exists()

def test_manifest_task_persists_through_lifecycle(tmp_path):
    """The workflow task propagates from run() into the finalized manifest."""
    def runner(req):
        return NodeRunResponse(output='x', session_key='sk')

    engine = WorkflowEngine(runner, workspace=str(tmp_path), run_id_factory=lambda: 'r1')
    res = engine.run(_two_node_wf(), 'process the annual budget', root_session_key='sess:1')
    assert res.status == 'completed'

    rec = run_log.read_manifest(tmp_path, 'w', 'r1')
    assert rec is not None
    assert rec['task'] == 'process the annual budget'


def test_manifest_parent_run_id_persists_through_lifecycle(tmp_path):
    """A top-level run passes no parent_run_id; a nested run's engine.run(parent_run_id=...)
    must land in both the running and the finalized manifest."""
    def runner(req):
        return NodeRunResponse(output="x", session_key="sk")

    engine = WorkflowEngine(runner, workspace=str(tmp_path), run_id_factory=lambda: "r1")
    res = engine.run(_two_node_wf(), "go", parent_run_id="parent1")
    assert res.status == "completed"

    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert rec["parent_run_id"] == "parent1"


def test_manifest_parent_run_id_defaults_to_none(tmp_path):
    def runner(req):
        return NodeRunResponse(output="x", session_key="sk")

    engine = WorkflowEngine(runner, workspace=str(tmp_path), run_id_factory=lambda: "r1")
    engine.run(_two_node_wf(), "go")
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert rec["parent_run_id"] is None


def test_finalize_manifest_prunes_older_terminal_runs(tmp_path):
    """_finalize_manifest calls prune_manifests(keep=self._prune_keep) best-effort right
    after a successful finalize_run, bounding manifest growth for this workflow name."""
    def runner(req):
        return NodeRunResponse(output="x", session_key="sk")

    run_ids = iter(["r0", "r1", "r2"])
    engine = WorkflowEngine(runner, workspace=str(tmp_path),
                            run_id_factory=lambda: next(run_ids), prune_keep=2)
    for _ in range(3):
        engine.run(_two_node_wf(), "go")

    remaining = {p.stem for p in (tmp_path / "workflows-runs" / "w").glob("*.json")}
    assert remaining == {"r1", "r2"}   # r0 pruned; the 2 most recent survive

