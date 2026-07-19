"""Tests for per-run workflow records (the diagnostic source)."""

from durin.workflow import run_log
from durin.workflow.result import NodeRun, WorkflowResult


def _result(run_id, status="completed", runs=None):
    return WorkflowResult(status=status, final_output="x", runs=runs or [], run_id=run_id)


def test_write_and_read_run_round_trips(tmp_path):
    res = _result("r1", runs=[
        NodeRun(node_id="a", iteration=2, output="o"),
        NodeRun(node_id="g", iteration=1, output="", passed=False),
    ])
    run_log.write_run(tmp_path, "wf", res, ts=100.0)
    got = run_log.read_runs_since(tmp_path, "wf")
    assert len(got) == 1
    rec = got[0]
    assert rec["run_id"] == "r1" and rec["status"] == "completed"
    by_node = {(r["node_id"], r["iteration"]): r for r in rec["runs"]}
    assert by_node[("a", 2)]["passed"] is None
    assert by_node[("g", 1)]["passed"] is False


def test_records_land_beside_workflows_not_inside(tmp_path):
    (tmp_path / "workflows").mkdir()
    run_log.write_run(tmp_path, "wf", _result("r1"), ts=1.0)
    # the version-store snapshots <workspace>/workflows; run records must not be there
    assert not list((tmp_path / "workflows").glob("**/*.json"))
    assert (tmp_path / "workflows-runs" / "wf" / "r1.json").exists()


def test_cursor_excludes_consumed_runs(tmp_path):
    run_log.write_run(tmp_path, "wf", _result("r1"), ts=10.0)
    run_log.write_run(tmp_path, "wf", _result("r2"), ts=20.0)
    run_log.advance_cursor(tmp_path, "wf", 10.0)
    fresh = run_log.read_runs_since(tmp_path, "wf", run_log.read_cursor(tmp_path, "wf"))
    assert [r["run_id"] for r in fresh] == ["r2"]   # r1 already consumed


def test_read_runs_sorted_oldest_first(tmp_path):
    run_log.write_run(tmp_path, "wf", _result("late"), ts=30.0)
    run_log.write_run(tmp_path, "wf", _result("early"), ts=5.0)
    assert [r["run_id"] for r in run_log.read_runs_since(tmp_path, "wf")] == ["early", "late"]


def test_names_with_runs(tmp_path):
    run_log.write_run(tmp_path, "alpha", _result("r1"), ts=1.0)
    run_log.write_run(tmp_path, "beta", _result("r2"), ts=1.0)
    assert run_log.workflow_names_with_runs(tmp_path) == ["alpha", "beta"]


def test_no_runs_is_empty(tmp_path):
    assert run_log.read_runs_since(tmp_path, "nope") == []
    assert run_log.read_cursor(tmp_path, "nope") == 0.0
    assert run_log.workflow_names_with_runs(tmp_path) == []


# --- live manifest (B1) -----------------------------------------------------


def test_start_run_writes_running_manifest(tmp_path):
    run_log.start_run(tmp_path, "wf", "r1", root_session_key="sess:1", started_at=100.0)
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec is not None
    assert rec["status"] == "running"
    assert rec["runs"] == []
    assert rec["root_session_key"] == "sess:1"
    assert rec["run_id"] == "r1"
    assert rec["started_at"] == 100.0


def test_update_run_reflects_node_records(tmp_path):
    run_log.start_run(tmp_path, "wf", "r1", root_session_key="sess:1", started_at=100.0)
    res = _result("r1", status="running", runs=[
        NodeRun(node_id="a", iteration=1, output="o", session_key="workflow:r1:a:1", status="ok"),
        NodeRun(node_id="g", iteration=1, output="", passed=False,
                session_key="workflow:r1:g:1", status="ok"),
    ])
    run_log.update_run(tmp_path, "wf", "r1", res)
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["status"] == "running"
    assert rec["root_session_key"] == "sess:1"   # preserved across update
    assert rec["started_at"] == 100.0            # preserved across update
    by_node = {r["node_id"]: r for r in rec["runs"]}
    assert by_node["a"]["session_key"] == "workflow:r1:a:1"
    assert by_node["a"]["status"] == "ok"
    assert by_node["g"]["passed"] is False


def test_finalize_run_writes_terminal_status(tmp_path):
    run_log.start_run(tmp_path, "wf", "r1", root_session_key="sess:1", started_at=100.0)
    res = _result("r1", status="completed", runs=[
        NodeRun(node_id="a", iteration=1, output="o", session_key="workflow:r1:a:1"),
    ])
    run_log.finalize_run(tmp_path, "wf", res, root_session_key="sess:1",
                         started_at=100.0, finished_at=130.0)
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["status"] == "completed"
    assert rec["finished_at"] == 130.0
    assert rec["runs"][0]["session_key"] == "workflow:r1:a:1"
    # the finalized record is dream-visible via read_runs_since (ts == finished_at)
    got = run_log.read_runs_since(tmp_path, "wf")
    assert [r["run_id"] for r in got] == ["r1"]


def test_finalize_records_needs_input_node(tmp_path):
    from durin.workflow.result import WorkflowResult
    result = WorkflowResult(status="needs_input", final_output="what env?",
                            runs=[], run_id="r9", needs_input_node="gate")
    run_log.finalize_run(tmp_path, "w", result, root_session_key=None,
                         started_at=1.0, finished_at=2.0)
    rec = run_log.read_manifest(tmp_path, "w", "r9")
    assert rec["needs_input_node"] == "gate"


def test_finalize_records_final_output_node(tmp_path):
    from durin.workflow.result import WorkflowResult
    result = WorkflowResult(status="completed", final_output="done", final_output_node="gate",
                            runs=[], run_id="r11")
    run_log.finalize_run(tmp_path, "w", result, root_session_key=None,
                         started_at=1.0, finished_at=2.0)
    rec = run_log.read_manifest(tmp_path, "w", "r11")
    assert rec["final_output_node"] == "gate"


def test_finalize_records_output_files(tmp_path):
    from durin.workflow.result import WorkflowResult
    result = WorkflowResult(status="completed", final_output="done",
                            runs=[], run_id="r10", output_files=["a.md"])
    run_log.finalize_run(tmp_path, "w", result, root_session_key=None,
                         started_at=1.0, finished_at=2.0)
    rec = run_log.read_manifest(tmp_path, "w", "r10")
    assert rec["output_files"] == ["a.md"]


def test_node_record_carries_budget(tmp_path):
    res = _result("r1", runs=[NodeRun(node_id="a", iteration=1, output="o", budget=3)])
    run_log.write_run(tmp_path, "wf", res, ts=100.0)
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["runs"][0]["budget"] == 3


def test_runs_for_session_matches_root_newest_first(tmp_path):
    run_log.finalize_run(tmp_path, "wf", _result("old"), root_session_key="sess:1",
                         started_at=1.0, finished_at=2.0)
    run_log.finalize_run(tmp_path, "wf", _result("new"), root_session_key="sess:1",
                         started_at=10.0, finished_at=20.0)
    run_log.finalize_run(tmp_path, "other", _result("nope"), root_session_key="sess:2",
                         started_at=5.0, finished_at=6.0)
    got = run_log.runs_for_session(tmp_path, "sess:1")
    assert [r["run_id"] for r in got] == ["new", "old"]   # newest-first


def _set_owner(tmp_path, name, run_id, owner):
    """Rewrite a manifest's owner in place (None = legacy ownerless record)."""
    import json as _json

    f = tmp_path / "workflows-runs" / name / f"{run_id}.json"
    rec = _json.loads(f.read_text(encoding="utf-8"))
    if owner is None:
        rec.pop("owner", None)
    else:
        rec["owner"] = owner
    f.write_text(_json.dumps(rec), encoding="utf-8")


_DEAD_OWNER = {"pid": 2**22 + 54321, "started": "never"}


def test_reconcile_flips_dead_owner_regardless_of_age(tmp_path):
    """The 2026-07-18 ghost: the owning gateway crashed and the run was only
    52 minutes old at the next boot — under any age cutoff. A dead owner is
    reason enough."""
    import time as _time

    run_log.start_run(tmp_path, "wf", "ghost", root_session_key="s",
                      started_at=_time.time())   # seconds old
    _set_owner(tmp_path, "wf", "ghost", _DEAD_OWNER)

    n = run_log.reconcile_running(
        tmp_path, now=_time.time(), max_age_s=run_log.RECONCILE_AGE_S)
    assert n == 1
    assert run_log.read_manifest(tmp_path, "wf", "ghost")["status"] == "crashed"


def test_reconcile_never_touches_live_owner(tmp_path):
    """A run owned by a LIVE process (this one) survives the sweep even when
    ancient — the workspace is multi-process (TUI + gateway) and the sweep
    must not kill a neighbour's healthy run."""
    run_log.start_run(tmp_path, "wf", "mine", root_session_key="s", started_at=0.0)

    n = run_log.reconcile_running(tmp_path, now=10**12, max_age_s=1.0)
    assert n == 0
    assert run_log.read_manifest(tmp_path, "wf", "mine")["status"] == "running"


def test_reconcile_legacy_ownerless_uses_age(tmp_path):
    run_log.start_run(tmp_path, "wf", "stale", root_session_key="s", started_at=0.0)
    run_log.start_run(tmp_path, "wf", "fresh", root_session_key="s", started_at=1950.0)
    _set_owner(tmp_path, "wf", "stale", None)
    _set_owner(tmp_path, "wf", "fresh", None)
    run_log.finalize_run(tmp_path, "wf", _result("done"), root_session_key="s",
                         started_at=0.0, finished_at=5.0)

    n = run_log.reconcile_running(tmp_path, now=2000.0, max_age_s=100.0)
    assert n == 1   # only the stale running one

    assert run_log.read_manifest(tmp_path, "wf", "stale")["status"] == "crashed"
    assert run_log.read_manifest(tmp_path, "wf", "fresh")["status"] == "running"
    assert run_log.read_manifest(tmp_path, "wf", "done")["status"] == "completed"


def test_reconcile_preserves_partial_runs_and_survives_malformed(tmp_path):
    res = _result("stale", status="running", runs=[
        NodeRun(node_id="a", iteration=1, output="o", session_key="workflow:stale:a:1"),
    ])
    run_log.start_run(tmp_path, "wf", "stale", root_session_key="s", started_at=0.0)
    run_log.update_run(tmp_path, "wf", "stale", res)
    _set_owner(tmp_path, "wf", "stale", _DEAD_OWNER)
    # A malformed record must not crash the sweep.
    (tmp_path / "workflows-runs" / "wf" / "junk.json").write_text("not json", encoding="utf-8")

    run_log.reconcile_running(tmp_path, now=2000.0, max_age_s=100.0)
    rec = run_log.read_manifest(tmp_path, "wf", "stale")
    assert rec["status"] == "crashed"
    assert rec["runs"][0]["session_key"] == "workflow:stale:a:1"   # partial trace kept


def test_start_and_update_carry_owner(tmp_path):
    run_log.start_run(tmp_path, "wf", "r1", root_session_key="s", started_at=1.0)
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    import os as _os

    assert rec["owner"]["pid"] == _os.getpid()
    run_log.update_run(tmp_path, "wf", "r1", _result("r1", status="running"))
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["owner"]["pid"] == _os.getpid()   # rewrite preserved it



def test_task_persists_through_start_update_finalize(tmp_path):
    """The task written by start_run survives update_run and finalize_run."""
    run_log.start_run(tmp_path, "wf", "r1", root_session_key="sess:1",
                      started_at=100.0, task="summarise the quarterly report")
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["task"] == "summarise the quarterly report"

    res = _result("r1", status="running", runs=[
        NodeRun(node_id="a", iteration=1, output="o", session_key="sk", status="ok"),
    ])
    run_log.update_run(tmp_path, "wf", "r1", res)
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["task"] == "summarise the quarterly report"

    run_log.finalize_run(tmp_path, "wf", _result("r1", runs=[
        NodeRun(node_id="a", iteration=1, output="o", session_key="sk"),
    ]), root_session_key="sess:1", started_at=100.0, finished_at=130.0)
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["task"] == "summarise the quarterly report"
    assert rec["status"] == "completed"


def test_task_none_when_omitted(tmp_path):
    """start_run without task defaults to None, no task key in the record."""
    run_log.start_run(tmp_path, "wf", "r2", root_session_key=None, started_at=1.0)
    rec = run_log.read_manifest(tmp_path, "wf", "r2")
    assert rec.get("task") is None


def test_read_runs_since_tolerates_old_schema(tmp_path):
    # A v1 on-disk record (no schema/root_session_key field, as written before the
    # manifest) is still returned by read_runs_since without error.
    import json
    d = tmp_path / "workflows-runs" / "wf"
    d.mkdir(parents=True)
    (d / "legacy.json").write_text(json.dumps({
        "run_id": "legacy", "workflow": "wf", "status": "completed", "ts": 50.0,
        "runs": [{"node_id": "a", "iteration": 1, "passed": None}],
    }), encoding="utf-8")
    got = run_log.read_runs_since(tmp_path, "wf")
    assert [r["run_id"] for r in got] == ["legacy"]
    assert "root_session_key" not in got[0]


def test_list_runs_newest_first_summaries(tmp_path):
    from durin.workflow.result import WorkflowResult
    run_log.finalize_run(tmp_path, "wf", WorkflowResult(
        status="completed", final_output="a" * 300, runs=[], run_id="old"),
        root_session_key=None, started_at=1.0, finished_at=2.0)
    run_log.finalize_run(tmp_path, "wf", WorkflowResult(
        status="needs_input", final_output="q", runs=[], run_id="new",
        needs_input_node="gate"),
        root_session_key=None, started_at=10.0, finished_at=20.0)
    got = run_log.list_runs(tmp_path, "wf")
    assert [r["run_id"] for r in got] == ["new", "old"]
    assert got[0]["status"] == "needs_input"
    assert got[0]["needs_input_node"] == "gate"
    assert got[1]["task"] == ""
    assert len(got[1]["task"]) <= 200


def test_list_runs_caps_task_at_200_chars(tmp_path):
    run_log.start_run(tmp_path, "wf", "r1", root_session_key=None, started_at=1.0,
                      task="x" * 500)
    got = run_log.list_runs(tmp_path, "wf")
    assert len(got[0]["task"]) == 200


def test_list_runs_respects_limit(tmp_path):
    for i in range(3):
        run_log.start_run(tmp_path, "wf", f"r{i}", root_session_key=None,
                          started_at=float(i))
    got = run_log.list_runs(tmp_path, "wf", limit=2)
    assert len(got) == 2
    assert [r["run_id"] for r in got] == ["r2", "r1"]


def test_list_runs_no_directory_is_empty(tmp_path):
    assert run_log.list_runs(tmp_path, "nope") == []


# --- prune_manifests (F6) ----------------------------------------------------


def test_prune_manifests_keeps_newest_terminal_and_deletes_older(tmp_path):
    for i in range(5):
        run_log.finalize_run(tmp_path, "wf", _result(f"r{i}"), root_session_key=None,
                             started_at=float(i), finished_at=float(i))
    run_log.prune_manifests(tmp_path, "wf", keep=2)
    remaining = {p.stem for p in (tmp_path / "workflows-runs" / "wf").glob("*.json")}
    assert remaining == {"r3", "r4"}   # the two newest by ts survive


def test_prune_manifests_never_deletes_running_or_needs_input(tmp_path):
    run_log.start_run(tmp_path, "wf", "live", root_session_key=None, started_at=0.0)
    from durin.workflow.result import WorkflowResult
    run_log.finalize_run(tmp_path, "wf", WorkflowResult(
        status="needs_input", final_output="q", runs=[], run_id="waiting",
        needs_input_node="gate"), root_session_key=None, started_at=1.0, finished_at=1.0)
    for i in range(5):
        run_log.finalize_run(tmp_path, "wf", _result(f"r{i}"), root_session_key=None,
                             started_at=float(i + 10), finished_at=float(i + 10))
    run_log.prune_manifests(tmp_path, "wf", keep=1)
    remaining = {p.stem for p in (tmp_path / "workflows-runs" / "wf").glob("*.json")}
    assert "live" in remaining
    assert "waiting" in remaining
    assert "r4" in remaining   # the single newest terminal record survives
    assert len(remaining) == 3


def test_prune_manifests_running_and_needs_input_do_not_count_against_keep(tmp_path):
    run_log.start_run(tmp_path, "wf", "live", root_session_key=None, started_at=0.0)
    for i in range(3):
        run_log.finalize_run(tmp_path, "wf", _result(f"r{i}"), root_session_key=None,
                             started_at=float(i + 10), finished_at=float(i + 10))
    run_log.prune_manifests(tmp_path, "wf", keep=3)
    remaining = {p.stem for p in (tmp_path / "workflows-runs" / "wf").glob("*.json")}
    assert remaining == {"live", "r0", "r1", "r2"}   # all 3 terminal fit within keep=3


def test_prune_manifests_skips_malformed_files(tmp_path):
    d = tmp_path / "workflows-runs" / "wf"
    d.mkdir(parents=True)
    (d / "junk.json").write_text("not json", encoding="utf-8")
    for i in range(3):
        run_log.finalize_run(tmp_path, "wf", _result(f"r{i}"), root_session_key=None,
                             started_at=float(i), finished_at=float(i))
    run_log.prune_manifests(tmp_path, "wf", keep=1)
    remaining = {p.stem for p in d.glob("*.json")}
    assert "junk" in remaining   # malformed file is skipped, never deleted
    assert "r2" in remaining


def test_prune_manifests_ignores_cursor_file(tmp_path):
    run_log.advance_cursor(tmp_path, "wf", 5.0)
    for i in range(3):
        run_log.finalize_run(tmp_path, "wf", _result(f"r{i}"), root_session_key=None,
                             started_at=float(i), finished_at=float(i))
    run_log.prune_manifests(tmp_path, "wf", keep=1)
    d = tmp_path / "workflows-runs" / "wf"
    assert (d / ".cursor.json").exists()


def test_prune_manifests_no_directory_is_a_noop(tmp_path):
    run_log.prune_manifests(tmp_path, "nope", keep=5)   # must not raise


def test_prune_manifests_survives_oserror(tmp_path, monkeypatch):
    for i in range(3):
        run_log.finalize_run(tmp_path, "wf", _result(f"r{i}"), root_session_key=None,
                             started_at=float(i), finished_at=float(i))

    def _boom(self, *a, **kw):
        raise OSError("disk gone")

    monkeypatch.setattr("pathlib.Path.unlink", _boom)
    run_log.prune_manifests(tmp_path, "wf", keep=0)   # best-effort: must not raise


# --- parent_run_id (F6) ------------------------------------------------------


def test_start_run_records_parent_run_id(tmp_path):
    run_log.start_run(tmp_path, "wf", "child1", root_session_key=None, started_at=1.0,
                      parent_run_id="parent1")
    rec = run_log.read_manifest(tmp_path, "wf", "child1")
    assert rec["parent_run_id"] == "parent1"


def test_start_run_parent_run_id_defaults_to_none(tmp_path):
    run_log.start_run(tmp_path, "wf", "r1", root_session_key=None, started_at=1.0)
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["parent_run_id"] is None


def test_finalize_run_records_parent_run_id(tmp_path):
    run_log.finalize_run(tmp_path, "wf", _result("r1"), root_session_key=None,
                         started_at=1.0, finished_at=2.0, parent_run_id="parent1")
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["parent_run_id"] == "parent1"


def test_update_run_preserves_parent_run_id(tmp_path):
    run_log.start_run(tmp_path, "wf", "r1", root_session_key=None, started_at=1.0,
                      parent_run_id="parent1")
    run_log.update_run(tmp_path, "wf", "r1", _result("r1", status="running"))
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["parent_run_id"] == "parent1"


def test_finalize_run_preserves_parent_run_id_from_start(tmp_path):
    # finalize_run does not take parent_run_id explicitly here; it must fall back to
    # what start_run recorded, mirroring how it already preserves `task`.
    run_log.start_run(tmp_path, "wf", "r1", root_session_key=None, started_at=1.0,
                      parent_run_id="parent1")
    run_log.finalize_run(tmp_path, "wf", _result("r1"), root_session_key=None,
                         started_at=1.0, finished_at=2.0)
    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["parent_run_id"] == "parent1"


def test_list_runs_includes_parent_run_id(tmp_path):
    run_log.finalize_run(tmp_path, "wf", _result("r1"), root_session_key=None,
                         started_at=1.0, finished_at=2.0, parent_run_id="parent1")
    run_log.finalize_run(tmp_path, "wf", _result("r2"), root_session_key=None,
                         started_at=3.0, finished_at=4.0)
    got = {r["run_id"]: r for r in run_log.list_runs(tmp_path, "wf")}
    assert got["r1"]["parent_run_id"] == "parent1"
    assert got["r2"]["parent_run_id"] is None


def test_list_runs_parent_run_id_absent_in_old_manifest_is_none(tmp_path):
    import json
    d = tmp_path / "workflows-runs" / "wf"
    d.mkdir(parents=True)
    (d / "legacy.json").write_text(json.dumps({
        "run_id": "legacy", "workflow": "wf", "status": "completed", "ts": 1.0, "runs": [],
    }), encoding="utf-8")
    got = run_log.list_runs(tmp_path, "wf")
    assert got[0]["parent_run_id"] is None


# --- dream-cursor degradation under pruning (F6) -----------------------------


def test_prune_below_unconsumed_cursor_dream_still_works(tmp_path):
    """Pruning is not coupled to the dream cursor: when more than `keep` terminal runs
    accumulate before a dream pass consumes them, older unconsumed records may be
    deleted. The surviving recent window must still feed read_runs_since and
    compute_diagnostics without crashing — fewer records, not a broken pass."""
    from durin.workflow.diagnostics import compute_diagnostics

    for i in range(5):
        run_log.finalize_run(tmp_path, "wf", _result(f"r{i}"), root_session_key=None,
                             started_at=float(i), finished_at=float(i))
    # The dream cursor has not advanced past any of these runs yet.
    cursor = run_log.read_cursor(tmp_path, "wf")
    assert cursor == 0.0

    run_log.prune_manifests(tmp_path, "wf", keep=2)   # r0..r2 pruned; r3, r4 survive

    records = run_log.read_runs_since(tmp_path, "wf", cursor)
    assert [r["run_id"] for r in records] == ["r3", "r4"]   # gap: r0-r2 silently gone
    diag = compute_diagnostics(records)
    assert diag.total_runs == 2   # no crash; diagnostics just sees fewer records


# --- list_all_runs (F8) -------------------------------------------------------


def test_list_all_runs_merges_across_workflows_newest_first(tmp_path):
    from durin.workflow.result import WorkflowResult

    run_log.finalize_run(tmp_path, "alpha", WorkflowResult(
        status="completed", final_output="a", runs=[], run_id="old"),
        root_session_key=None, started_at=1.0, finished_at=2.0)
    run_log.finalize_run(tmp_path, "beta", WorkflowResult(
        status="completed", final_output="b", runs=[], run_id="new"),
        root_session_key=None, started_at=10.0, finished_at=20.0)
    got = run_log.list_all_runs(tmp_path)
    assert [r["run_id"] for r in got] == ["new", "old"]
    assert {r["workflow"] for r in got} == {"alpha", "beta"}


def test_list_all_runs_each_entry_carries_its_workflow_name(tmp_path):
    from durin.workflow.result import WorkflowResult

    run_log.finalize_run(tmp_path, "alpha", WorkflowResult(
        status="completed", final_output="a", runs=[], run_id="r1"),
        root_session_key=None, started_at=1.0, finished_at=2.0)
    got = run_log.list_all_runs(tmp_path)
    assert got[0]["workflow"] == "alpha"
    assert got[0]["run_id"] == "r1"


def test_list_all_runs_respects_limit_for_terminal_entries(tmp_path):
    from durin.workflow.result import WorkflowResult

    for i in range(5):
        run_log.finalize_run(tmp_path, "wf", WorkflowResult(
            status="completed", final_output="x", runs=[], run_id=f"r{i}"),
            root_session_key=None, started_at=float(i), finished_at=float(i))
    got = run_log.list_all_runs(tmp_path, limit=2)
    assert len(got) == 2
    assert [r["run_id"] for r in got] == ["r4", "r3"]


def test_list_all_runs_needs_input_exempt_from_cap(tmp_path):
    """needs_input entries are always included, even beyond `limit` — they are
    actionable resume points the tray must never lose to the cap."""
    from durin.workflow.result import WorkflowResult

    run_log.finalize_run(tmp_path, "wf", WorkflowResult(
        status="needs_input", final_output="q?", runs=[], run_id="waiting",
        needs_input_node="gate"), root_session_key=None, started_at=0.0, finished_at=0.0)
    for i in range(3):
        run_log.finalize_run(tmp_path, "wf", WorkflowResult(
            status="completed", final_output="x", runs=[], run_id=f"r{i}"),
            root_session_key=None, started_at=float(i + 10), finished_at=float(i + 10))
    got = run_log.list_all_runs(tmp_path, limit=1)
    ids = [r["run_id"] for r in got]
    assert "waiting" in ids            # exempt from the cap
    assert len(ids) == 2               # 1 terminal (cap) + the needs_input entry
    assert ids == ["r2", "waiting"]    # still newest-first overall (r2 ts=12 > waiting ts=0)


def test_list_all_runs_questions_field_on_needs_input_capped_at_500(tmp_path):
    from durin.workflow.result import WorkflowResult

    run_log.finalize_run(tmp_path, "wf", WorkflowResult(
        status="needs_input", final_output="q" * 900, runs=[], run_id="r1",
        needs_input_node="gate"), root_session_key=None, started_at=1.0, finished_at=1.0)
    got = run_log.list_all_runs(tmp_path)
    assert got[0]["questions"] == "q" * 500


def test_list_all_runs_terminal_entries_have_no_questions_field(tmp_path):
    from durin.workflow.result import WorkflowResult

    run_log.finalize_run(tmp_path, "wf", WorkflowResult(
        status="completed", final_output="done", runs=[], run_id="r1"),
        root_session_key=None, started_at=1.0, finished_at=1.0)
    got = run_log.list_all_runs(tmp_path)
    assert "questions" not in got[0]


def test_list_all_runs_no_workflows_is_empty(tmp_path):
    assert run_log.list_all_runs(tmp_path) == []


def test_legacy_needs_input_without_reentry_node_is_prunable(tmp_path):
    # A needs_input manifest WITHOUT needs_input_node (written before the resume
    # feature) is not a resume point — the resume endpoints reject it. It must
    # count as terminal for pruning instead of living forever as an unactionable
    # ghost; a RESUMABLE needs_input (node set) stays protected.
    for i in range(3):
        result = WorkflowResult(status="completed", final_output="done",
                                runs=[], run_id=f"t{i}")
        run_log.finalize_run(tmp_path, "w", result, root_session_key=None,
                             started_at=float(i), finished_at=float(i))
    legacy = WorkflowResult(status="needs_input", final_output="", runs=[], run_id="ghost")
    run_log.finalize_run(tmp_path, "w", legacy, root_session_key=None,
                         started_at=0.5, finished_at=0.5)
    resumable = WorkflowResult(status="needs_input", final_output="q?",
                               runs=[], run_id="live", needs_input_node="gate")
    run_log.finalize_run(tmp_path, "w", resumable, root_session_key=None,
                         started_at=0.6, finished_at=0.6)

    run_log.prune_manifests(tmp_path, "w", keep=2)

    assert run_log.read_manifest(tmp_path, "w", "ghost") is None       # legacy pruned (oldest beyond keep)
    assert run_log.read_manifest(tmp_path, "w", "live") is not None    # resumable never pruned
    kept = [f"t{i}" for i in range(3) if run_log.read_manifest(tmp_path, "w", f"t{i}")]
    assert kept == ["t1", "t2"]                                        # newest 2 terminals kept


def test_manifest_carries_work_dir_and_duration(tmp_path):
    run_log.start_run(tmp_path, "wf", "r9", root_session_key=None, started_at=1.0,
                      task="t", work_dir=str(tmp_path / "wd"))
    res = _result("r9", status="running",
                  runs=[NodeRun(node_id="a", iteration=1, output="", duration_s=2.5)])
    run_log.update_run(tmp_path, "wf", "r9", res)
    m = run_log.read_manifest(tmp_path, "wf", "r9")
    assert m["work_dir"] == str(tmp_path / "wd")
    assert m["runs"][0]["duration_s"] == 2.5
    final = _result("r9", runs=res.runs)
    run_log.finalize_run(tmp_path, "wf", final, root_session_key=None,
                         started_at=1.0, finished_at=2.0)
    m = run_log.read_manifest(tmp_path, "wf", "r9")
    assert m["work_dir"] == str(tmp_path / "wd")   # survives the terminal rewrite
    assert m["runs"][0]["duration_s"] == 2.5
