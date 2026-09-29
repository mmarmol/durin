from types import SimpleNamespace

from durin.workflow import run_log


def _finish(tmp_path, name, run_id, rows, status="completed"):
    # A row is (node_id, duration) or (node_id, duration, node_status) — the third
    # element lets a test seed a "reused" row alongside ordinary "ok" ones.
    def _row(spec):
        n, d, *rest = spec
        return SimpleNamespace(
            node_id=n, iteration=1, passed=None, session_key=None, worker_index=None,
            branch_id=None, budget=None, status=(rest[0] if rest else "ok"),
            route_label=None, exit_code=None, duration_s=d, error=None,
        )

    result = SimpleNamespace(
        run_id=run_id, status=status, final_output="", final_output_node=None,
        needs_input_node=None, output_files=[], missing_artifacts=[],
        runs=[_row(spec) for spec in rows],
    )
    run_log.finalize_run(tmp_path, name, result, root_session_key=None,
                         started_at=0.0, finished_at=1.0)


def test_typical_is_the_median_across_completed_runs(tmp_path):
    _finish(tmp_path, "wf", "r1", [("scan", 10.0)])
    _finish(tmp_path, "wf", "r2", [("scan", 20.0)])
    _finish(tmp_path, "wf", "r3", [("scan", 90.0)])
    assert run_log.typical_node_durations(tmp_path, "wf") == {"scan": 20.0}


def test_typical_ignores_runs_that_did_not_complete(tmp_path):
    _finish(tmp_path, "wf", "r1", [("scan", 10.0)])
    _finish(tmp_path, "wf", "r2", [("scan", 999.0)], status="aborted")
    assert run_log.typical_node_durations(tmp_path, "wf") == {"scan": 10.0}


def test_typical_skips_nodes_without_a_duration(tmp_path):
    _finish(tmp_path, "wf", "r1", [("scan", 10.0), ("gate", None)])
    assert run_log.typical_node_durations(tmp_path, "wf") == {"scan": 10.0}


def test_typical_is_empty_for_a_workflow_with_no_history(tmp_path):
    assert run_log.typical_node_durations(tmp_path, "never-run") == {}


def test_typical_ignores_reused_node_rows(tmp_path):
    # A reused row's duration must never enter the sample, regardless of its
    # value — the row records how long the SKIP took (engine always writes
    # 0.0), not what a fresh dispatch would have cost. An unrealistic value
    # here proves the row is excluded by status, not merely harmless at 0.0.
    _finish(tmp_path, "wf", "r1", [("scan", 10.0)])
    _finish(tmp_path, "wf", "r2", [("scan", 20.0)])
    _finish(tmp_path, "wf", "r3", [("scan", 999.0, "reused")])
    assert run_log.typical_node_durations(tmp_path, "wf") == {"scan": 15.0}


def test_typical_total_does_not_sum_branches_no_single_run_takes(tmp_path):
    """The per-node medians span every branch prior runs took; one run takes one
    of them. Summing them estimates a path that cannot happen."""
    # Three prior runs of a router, all down the same branch.
    _finish(tmp_path, "router", "r1", [("route", 5.0), ("branch-a", 500.0)])
    _finish(tmp_path, "router", "r2", [("route", 5.0), ("branch-a", 520.0)])
    _finish(tmp_path, "router", "r3", [("route", 5.0), ("branch-a", 900.0)])

    assert run_log.typical_total_duration(tmp_path, "router") == 525.0


def test_typical_total_is_absent_when_prior_runs_took_different_routes(tmp_path):
    """A router whose runs skip, answer a question, or investigate in full has no
    single typical total: the median of a 0 s skip, a 6-minute answer and a
    30-minute investigation describes none of them."""
    _finish(tmp_path, "router", "r1", [("route", 0.2)])
    _finish(tmp_path, "router", "r2", [("route", 0.2), ("answer", 360.0)])
    _finish(tmp_path, "router", "r3", [("route", 0.2), ("investigate", 1800.0),
                                       ("answer", 300.0)])

    assert run_log.typical_total_duration(tmp_path, "router") is None
    # Each node's own median still compares like with like.
    assert run_log.typical_node_durations(tmp_path, "router") == {
        "route": 0.2, "answer": 330.0, "investigate": 1800.0}


def test_typical_total_treats_extra_loop_passes_as_the_same_route(tmp_path):
    """A revision loop walks the same nodes, just more often: its runs still
    estimate one another."""
    _finish(tmp_path, "loop", "r1", [("draft", 10.0), ("judge", 5.0)])
    _finish(tmp_path, "loop", "r2", [("draft", 10.0), ("judge", 5.0),
                                     ("draft", 10.0), ("judge", 5.0)])
    _finish(tmp_path, "loop", "r3", [("draft", 12.0), ("judge", 5.0)])

    assert run_log.typical_total_duration(tmp_path, "loop") == 17.0


def test_typical_total_counts_every_pass_of_a_looping_node(tmp_path):
    """A node visited three times contributes one median to typical_s but all
    three passes to the run's own total."""
    _finish(tmp_path, "loop", "r1", [("produce", 10.0), ("produce", 10.0), ("produce", 10.0)])
    assert run_log.typical_total_duration(tmp_path, "loop") == 30.0


def test_typical_total_ignores_runs_that_did_not_complete(tmp_path):
    _finish(tmp_path, "wf", "r1", [("scan", 10.0)])
    _finish(tmp_path, "wf", "r2", [("scan", 999.0)], status="aborted")
    assert run_log.typical_total_duration(tmp_path, "wf") == 10.0


def test_typical_total_is_absent_without_history(tmp_path):
    assert run_log.typical_total_duration(tmp_path, "never-run") is None


def test_typical_total_is_absent_when_no_run_measured_anything(tmp_path):
    _finish(tmp_path, "gates-only", "r1", [("gate", None)])
    assert run_log.typical_total_duration(tmp_path, "gates-only") is None


def test_typical_total_ignores_reused_node_rows(tmp_path):
    _finish(tmp_path, "wf", "r1", [("scan", 10.0), ("gate", 5.0)])
    _finish(tmp_path, "wf", "r2", [("scan", 500.0, "reused"), ("gate", 5.0)])
    # r1 totals 15.0; r2's reused "scan" row is excluded, so r2 totals 5.0 (just
    # "gate") — not 505.0. median(15.0, 5.0) = 10.0.
    assert run_log.typical_total_duration(tmp_path, "wf") == 10.0


def test_the_engine_records_the_typical_total_on_the_start_manifest(tmp_path):
    """Computed once at run start, like typical_s, so every reader shows the same
    number for the run's whole life instead of recomputing it."""
    from durin.workflow.engine import NodeRunResponse, WorkflowEngine
    from durin.workflow.spec import parse_workflow

    _finish(tmp_path, "wf", "prior", [("a", 12.0), ("b", 8.0)])
    wf = parse_workflow({"name": "wf", "start": "a",
                         "nodes": [{"id": "a", "kind": "work", "next": None}]})
    eng = WorkflowEngine(
        node_runner=lambda req: NodeRunResponse(output="x", session_key=None, messages=[]),
        run_id_factory=lambda: "r-new",
        workspace=str(tmp_path),
    )
    eng.run(wf, "go")

    assert run_log.read_manifest(tmp_path, "wf", "r-new")["typical_total_s"] == 20.0


def test_typical_total_leaves_out_a_run_resumed_after_a_failure(tmp_path):
    """A resume rewrites the run's manifest with the resumed walk's rows only, so
    a run that failed at b and resumed there keeps rows for b and c alone: its
    route and its total are both unknown. Counted anyway, it read as a different
    route from the fresh runs of the same graph and erased their estimate."""
    from durin.workflow.engine import (
        NodeExecutionError,
        NodeRunResponse,
        WorkflowEngine,
        build_resume_state,
    )
    from durin.workflow.spec import parse_workflow

    wf = parse_workflow({"name": "wf", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "b"},
        {"id": "b", "kind": "work", "next": "c"},
        {"id": "c", "kind": "work", "next": None},
    ]})
    fail_b = [False]

    def node_runner(req):
        if req.node.id == "b" and fail_b[0]:
            fail_b[0] = False
            raise NodeExecutionError("b", req.iteration, None, RuntimeError("transient"))
        return NodeRunResponse(output=f"{req.node.id}-out")

    ids = iter(["fresh", "resumed"])
    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: next(ids),
                         workspace=str(tmp_path))
    assert eng.run(wf, "t").status == "completed"
    fail_b[0] = True
    assert eng.run(wf, "t").status == "aborted"
    resume = build_resume_state(run_log.read_manifest(tmp_path, "wf", "resumed"), "")
    assert eng.run(wf, "t", resume=resume).status == "completed"

    resumed_rows = run_log.read_manifest(tmp_path, "wf", "resumed")["runs"]
    assert [r["node_id"] for r in resumed_rows] == ["b", "c"]   # a is gone
    fresh_rows = run_log.read_manifest(tmp_path, "wf", "fresh")["runs"]
    assert run_log.typical_total_duration(tmp_path, "wf") == sum(
        r["duration_s"] for r in fresh_rows)


def test_typical_total_leaves_out_a_run_resumed_past_an_approval(tmp_path):
    """An approved pause resumes at the node after the approval, so the rows kept
    for that run name only what came after it: a run whose route is unknown must
    not estimate the others."""
    from durin.workflow.approval import build_approval_resume
    from durin.workflow.engine import NodeRunResponse, WorkflowEngine
    from durin.workflow.spec import parse_workflow

    wf = parse_workflow({"name": "wf", "start": "draft", "nodes": [
        {"id": "draft", "kind": "work", "approval": True, "next": "send"},
        {"id": "send", "kind": "work", "next": None},
    ]})
    eng = WorkflowEngine(node_runner=lambda req: NodeRunResponse(output=req.node.id),
                         run_id_factory=lambda: "approved", workspace=str(tmp_path))
    assert eng.run(wf, "t").status == "needs_input"
    manifest = run_log.read_manifest(tmp_path, "wf", "approved")
    resume = build_approval_resume(wf, manifest, "approve", "")
    assert eng.run(wf, "t", resume=resume).status == "completed"

    approved_rows = run_log.read_manifest(tmp_path, "wf", "approved")["runs"]
    assert [r["node_id"] for r in approved_rows] == ["send"]   # draft is gone
    assert run_log.typical_total_duration(tmp_path, "wf") is None
