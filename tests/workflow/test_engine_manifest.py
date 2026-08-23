"""The engine owns the run manifest: it writes a ``running`` record before the walk,
updates it after each node, and finalizes it at the end — so a run is durable, observable
in-flight, and forward-referenceable from its calling session."""

from durin.workflow import run_log
from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.spec import parse_workflow


def test_manifest_records_final_route_label(tmp_path):
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "g"},
            {"id": "g", "kind": "work", "cases": {"DONE": None, "MORE": "a"}},
        ],
    })
    outputs = {"a": "out-a", "g": "verdict\nDONE"}

    def runner(req):
        return NodeRunResponse(output=outputs[req.node.id])

    eng = WorkflowEngine(runner, workspace=str(tmp_path), run_id_factory=lambda: "r1")
    res = eng.run(wf, "t")
    manifest = run_log.read_manifest(tmp_path, "d", res.run_id)
    assert manifest["final_route_label"] == "DONE"


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



def test_engine_records_work_dir_and_durations(tmp_path):
    def runner(req):
        return NodeRunResponse(output=f"out {req.node.id}",
                               session_key=f"workflow:r1:{req.node.id}:1")

    engine = WorkflowEngine(runner, workspace=str(tmp_path),
                            run_id_factory=lambda: "r1")
    res = engine.run(_two_node_wf(), "go")
    assert res.status == "completed"
    m = run_log.read_manifest(tmp_path, "w", "r1")
    assert m["work_dir"].endswith(".workflow/r1/work")
    assert len(m["runs"]) == 2
    assert all(isinstance(r["duration_s"], float) for r in m["runs"])


# ---------------------------------------------------------------------------
# Producer identity: spec_hash/durin_version at top level, model/provider/
# node_hash per node record
# ---------------------------------------------------------------------------

def test_manifest_carries_spec_hash_and_durin_version(tmp_path):
    def runner(req):
        return NodeRunResponse(output=f"out {req.node.id}", model="test-model", provider="test-provider")

    engine = WorkflowEngine(runner, workspace=str(tmp_path), run_id_factory=lambda: "r1")
    res = engine.run(_two_node_wf(), "go")
    assert res.status == "completed"

    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert rec["spec_hash"]                 # a real workflow → non-empty hash
    assert "durin_version" in rec           # value may be None depending on install

    by_node = {r["node_id"]: r for r in rec["runs"]}
    assert by_node["a"]["model"] == "test-model"
    assert by_node["a"]["provider"] == "test-provider"
    assert by_node["a"]["node_hash"]


def test_manifest_spec_hash_survives_mid_walk_update(tmp_path):
    # spec_hash/durin_version are written by start_run; update_run must carry
    # them forward, not drop them on the first mid-walk rewrite.
    seen = {}

    def runner(req):
        if req.node.id == "b":
            mid = run_log.read_manifest(tmp_path, "w", "r1")
            seen["spec_hash"] = mid.get("spec_hash")
        return NodeRunResponse(output=f"out {req.node.id}")

    WorkflowEngine(runner, workspace=str(tmp_path),
                   run_id_factory=lambda: "r1").run(_two_node_wf(), "go")
    assert seen["spec_hash"]


def test_spec_hash_covers_script_nodes_and_is_deterministic(tmp_path):
    """spec_hash must move when a NON-agent node's definition changes (a script
    node carries no `raw` field, so it is the case most likely to be silently
    skipped), and must be identical for two runs of the identical definition."""
    def runner(req):
        return NodeRunResponse(output=f"out {req.node.id}")

    def _spec_hash_for(command, run_id):
        wf = parse_workflow({
            "name": "w", "start": "a",
            "nodes": [{"id": "a", "kind": "work", "next": "b"},
                     {"id": "b", "kind": "script", "command": command, "next": None}],
        })
        engine = WorkflowEngine(runner, script_runner=runner, workspace=str(tmp_path),
                                run_id_factory=lambda: run_id)
        res = engine.run(wf, "go")
        assert res.status == "completed"
        return run_log.read_manifest(tmp_path, "w", run_id)["spec_hash"]

    h1 = _spec_hash_for("echo hi", "r1")
    h2 = _spec_hash_for("echo bye", "r2")
    h1_again = _spec_hash_for("echo hi", "r3")
    assert h1 != h2          # the script node's command IS covered by spec_hash
    assert h1 == h1_again    # deterministic: the identical spec parsed twice → same hash


def test_legacy_manifest_without_producer_fields_round_trips(tmp_path):
    """A manifest written before this change (no spec_hash/durin_version/model/
    provider/node_hash) must still round-trip through every public reader."""
    import json

    d = tmp_path / "workflows-runs" / "w"
    d.mkdir(parents=True)
    legacy = {
        "schema": 2, "run_id": "old", "workflow": "w", "status": "completed",
        "root_session_key": None, "started_at": 1.0, "finished_at": 2.0, "ts": 2.0,
        "task": None, "parent_run_id": None, "work_dir": None,
        "typical_s": {}, "typical_total_s": None,
        "final_output": "done", "final_output_node": "b",
        "needs_input_node": None, "failed_node": None,
        "resume_inputs": None, "resume_upstream": None,
        "output_files": [], "missing_artifacts": [],
        "runs": [{"node_id": "a", "iteration": 1, "passed": None, "session_key": None,
                  "worker_index": None, "branch_id": None, "budget": 3, "status": "ok",
                  "route_label": None, "exit_code": None, "duration_s": 0.1,
                  "error": None, "artifacts": [], "command": None, "stdout": None, "stderr": None}],
    }
    (d / "old.json").write_text(json.dumps(legacy), encoding="utf-8")
    assert "spec_hash" not in legacy        # confirms this really is the pre-change shape

    rec = run_log.read_manifest(tmp_path, "w", "old")
    assert rec["run_id"] == "old"

    summaries = run_log.list_runs(tmp_path, "w")
    assert [s["run_id"] for s in summaries] == ["old"]

    all_runs = run_log.list_all_runs(tmp_path)
    assert [s["run_id"] for s in all_runs] == ["old"]

    durations = run_log.typical_node_durations(tmp_path, "w")
    assert durations["a"] == 0.1


# ---------------------------------------------------------------------------
# on_run_end (PR-K round 2 / ITEM 2): a caller's cleanup hook, called
# synchronously BEFORE the terminal manifest write — not "soon after" engine.run()
# returns. A caller polling the manifest directly (tasks(action='stop'), a
# status check) becomes observably terminal to that caller ONLY after this
# hook has already run, by program order within this one thread — no race
# window, unlike clearing after engine.run() returns (which requires this
# thread to actually get scheduled again, with no real-time guarantee under
# contention from other concurrently-running threads).
# ---------------------------------------------------------------------------


def test_on_run_end_runs_before_the_manifest_becomes_terminal(tmp_path):
    order = []

    def runner(req):
        return NodeRunResponse(output=f"out {req.node.id}")

    real_finalize = run_log.finalize_run

    def spy_finalize_run(*a, **kw):
        order.append("manifest_write")
        return real_finalize(*a, **kw)

    import durin.workflow.run_log as run_log_mod
    run_log_mod.finalize_run = spy_finalize_run
    try:
        engine = WorkflowEngine(
            runner, workspace=str(tmp_path), run_id_factory=lambda: "r1",
            on_run_end=lambda run_id: order.append(("on_run_end", run_id)),
        )
        res = engine.run(_two_node_wf(), "go")
    finally:
        run_log_mod.finalize_run = real_finalize

    assert res.status == "completed"
    assert order == [("on_run_end", "r1"), "manifest_write"]


def test_on_run_end_fires_even_without_a_workspace():
    def runner(req):
        return NodeRunResponse(output="x")

    seen = []
    engine = WorkflowEngine(runner, run_id_factory=lambda: "r1",
                            on_run_end=lambda run_id: seen.append(run_id))
    res = engine.run(_two_node_wf(), "go")
    assert res.status == "completed"
    assert seen == ["r1"]


def test_on_run_end_exception_does_not_break_the_run(tmp_path):
    def runner(req):
        return NodeRunResponse(output="x")

    def _boom(run_id):
        raise RuntimeError("boom")

    engine = WorkflowEngine(runner, workspace=str(tmp_path), run_id_factory=lambda: "r1",
                            on_run_end=_boom)
    res = engine.run(_two_node_wf(), "go")
    assert res.status == "completed"
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert rec["status"] == "completed"
