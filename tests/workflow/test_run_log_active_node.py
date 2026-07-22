from durin.workflow import run_log
from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.spec import parse_workflow


def test_mark_node_started_records_the_active_node(tmp_path):
    run_log.start_run(tmp_path, "wf", "r1", root_session_key=None, started_at=100.0)
    run_log.mark_node_started(tmp_path, "wf", "r1", node_id="scan", label="Scan", started_at=140.0)

    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["active_node"] == {"node_id": "scan", "label": "Scan", "started_at": 140.0}
    # The rest of the running manifest survives the rewrite.
    assert rec["status"] == "running"
    assert rec["started_at"] == 100.0


def test_mark_node_started_is_a_noop_without_a_manifest(tmp_path):
    # A nested/headless path may not have written one; must not raise or create a file.
    run_log.mark_node_started(tmp_path, "wf", "missing", node_id="a", label="A", started_at=1.0)
    assert run_log.read_manifest(tmp_path, "wf", "missing") is None


def test_update_run_clears_the_active_node(tmp_path):
    from types import SimpleNamespace

    run_log.start_run(tmp_path, "wf", "r2", root_session_key=None, started_at=100.0)
    run_log.mark_node_started(tmp_path, "wf", "r2", node_id="scan", label="Scan", started_at=140.0)
    result = SimpleNamespace(runs=[])
    run_log.update_run(tmp_path, "wf", "r2", result)

    # The node finished; leaving it marked would pin a finished node as "running".
    assert run_log.read_manifest(tmp_path, "wf", "r2").get("active_node") is None


def test_finalize_does_not_carry_a_stale_active_node_forward(tmp_path):
    """finalize_run deliberately preserves work_dir/task/typical_s from the
    running manifest. active_node must never join them: a terminal run has
    nothing in flight, and a marker that survives finalization reads as a node
    still running long after its run ended."""
    from types import SimpleNamespace

    run_log.start_run(tmp_path, "wf", "r3", root_session_key=None, started_at=100.0)
    run_log.mark_node_started(tmp_path, "wf", "r3", node_id="scan", label="Scan",
                              started_at=140.0)
    result = SimpleNamespace(
        run_id="r3", status="aborted", final_output="", final_output_node=None,
        needs_input_node=None, output_files=[], missing_artifacts=[], runs=[],
    )
    run_log.finalize_run(tmp_path, "wf", result, root_session_key=None,
                         started_at=100.0, finished_at=200.0)

    assert run_log.read_manifest(tmp_path, "wf", "r3").get("active_node") is None


_WF = parse_workflow({"name": "wf", "start": "scan",
                      "nodes": [{"id": "scan", "kind": "work", "title": "Scan", "next": None}]})


def test_a_real_run_marks_its_node_started_and_clears_it_when_done(tmp_path):
    """The only proof that the engine writes active_node at all. Without it, a
    change to what finalize_run carries forward (as it already does for work_dir
    and task) could silently reintroduce a run whose dead node reads as running."""
    seen: dict = {}

    def runner(req):
        # Read the manifest from INSIDE the node's turn — that is the whole
        # point of the marker: a reader arriving mid-node.
        seen["mid_node"] = run_log.read_manifest(tmp_path, "wf", "r-live")["active_node"]
        return NodeRunResponse(output="x", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=runner, run_id_factory=lambda: "r-live",
                         workspace=str(tmp_path))
    assert eng.run(_WF, "go").status == "completed"

    assert seen["mid_node"]["node_id"] == "scan"
    assert seen["mid_node"]["label"] == "Scan"
    assert seen["mid_node"]["started_at"] > 0
    # The run is over: nothing is in flight, on any exit path.
    assert run_log.read_manifest(tmp_path, "wf", "r-live").get("active_node") is None


def test_a_workspaceless_engine_never_reaches_for_a_manifest(monkeypatch):
    """mark_node_started(None, …) raises, and the walk's handler would swallow it
    on every node of every workspace-less run — burying any real write failure."""
    calls = []
    monkeypatch.setattr(run_log, "mark_node_started",
                        lambda *a, **k: calls.append((a, k)))

    eng = WorkflowEngine(
        node_runner=lambda req: NodeRunResponse(output="x", session_key=None, messages=[]),
        run_id_factory=lambda: "r-nows",
    )
    assert eng.run(_WF, "go").status == "completed"
    assert calls == []
