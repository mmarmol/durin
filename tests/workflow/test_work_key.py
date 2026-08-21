"""work_key: the production entrance for the reuse gate. A caller-supplied key
picks a STABLE working folder (``.workflow/keys/<workflow>/<key>/work``) instead of
the fresh-per-run default, so a later run of the same workflow with the same key
finds the provenance an earlier run stamped — the gate is unreachable without one of
these entrances (work_key, loop re-entry/resume, or a subworkflow's work_dir_override),
since a fresh run_id otherwise always starts with an empty ledger.

Concurrency: two runs sharing a work_key share one folder — one shared
provenance ledger and one output_file. Without serialization, two concurrent
same-key runs race: both read "no provenance yet", both dispatch, both write
output_file, last writer wins (a lost update) — reachable in production via a
loop with concurrency:"parallel" double-firing on the same correlate key. See
the concurrency section below for the fix under test (engine.run acquires a
cross_process_lock on the keyed dir for the whole run when work_key is set)."""

import threading
from pathlib import Path

import pytest

from durin.workflow import provenance, run_log
from durin.workflow.engine import NodeRunResponse, WorkflowEngine, build_resume_state
from durin.workflow.spec import parse_workflow


def _wf():
    return parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": None}]})


def _runner(req):
    return NodeRunResponse(output=f"out {req.node.id}")


def test_work_key_gives_a_stable_work_dir_across_separate_runs(tmp_path):
    run_ids = iter(["r1", "r2"])
    engine = WorkflowEngine(_runner, workspace=str(tmp_path), run_id_factory=lambda: next(run_ids))
    first = engine.run(_wf(), "t", work_key="ticket-1")
    second = engine.run(_wf(), "t", work_key="ticket-1")
    assert first.run_id != second.run_id           # two distinct runs...
    assert first.output_dir == second.output_dir    # ...sharing ONE working folder
    assert first.output_dir == str(
        tmp_path / ".workflow" / "keys" / "w-50e721e4" / "ticket-1-737ce60f" / "work")


def test_different_work_key_gives_different_work_dirs(tmp_path):
    run_ids = iter(["r1", "r2"])
    engine = WorkflowEngine(_runner, workspace=str(tmp_path), run_id_factory=lambda: next(run_ids))
    a = engine.run(_wf(), "t", work_key="ticket-1")
    b = engine.run(_wf(), "t", work_key="ticket-2")
    assert a.output_dir != b.output_dir


def test_no_work_key_keeps_todays_per_run_folder(tmp_path):
    run_ids = iter(["r1", "r2"])
    engine = WorkflowEngine(_runner, workspace=str(tmp_path), run_id_factory=lambda: next(run_ids))
    first = engine.run(_wf(), "t")
    second = engine.run(_wf(), "t")
    assert first.output_dir != second.output_dir     # unchanged behavior: fresh folder per run
    assert first.output_dir == str(tmp_path / ".workflow" / "r1" / "work")
    assert second.output_dir == str(tmp_path / ".workflow" / "r2" / "work")


def test_work_dir_override_wins_over_work_key(tmp_path):
    # Precedence: work_dir_override (subworkflows) > work_key > per-run default.
    override = tmp_path / "explicit-dir"
    override.mkdir()
    engine = WorkflowEngine(_runner, workspace=str(tmp_path), run_id_factory=lambda: "r1")
    result = engine.run(_wf(), "t", work_key="ticket-1", work_dir_override=str(override))
    assert result.output_dir == str(override)
    assert not (tmp_path / ".workflow" / "keys").exists()


def test_work_key_sanitizes_workflow_name_and_key(tmp_path):
    def _runner_named(req):
        return NodeRunResponse(output="x")

    wf = parse_workflow({"name": "My Workflow!", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": None}]})
    engine = WorkflowEngine(_runner_named, workspace=str(tmp_path), run_id_factory=lambda: "r1")
    result = engine.run(wf, "t", work_key="Ticket #23124")
    assert result.output_dir == str(
        tmp_path / ".workflow" / "keys" / "my_workflow_-c21cf4e9" / "ticket__23124-7e5e630c" / "work"
    )


def test_work_key_rejects_path_traversal(tmp_path):
    engine = WorkflowEngine(_runner, workspace=str(tmp_path), run_id_factory=lambda: "r1")
    with pytest.raises(ValueError):
        engine.run(_wf(), "t", work_key="../evil")


def test_work_key_rejects_empty_string(tmp_path):
    engine = WorkflowEngine(_runner, workspace=str(tmp_path), run_id_factory=lambda: "r1")
    with pytest.raises(ValueError):
        engine.run(_wf(), "t", work_key="")


def test_work_key_ignored_without_a_workspace():
    # A read-only engine (no workspace) has no folder to key — work_key is a no-op,
    # not an error, matching how work_dir_override / work_dir behave with no workspace.
    engine = WorkflowEngine(_runner, run_id_factory=lambda: "r1")
    result = engine.run(_wf(), "t", work_key="ticket-1")
    assert result.status == "completed"
    assert result.output_dir is None


def test_manifest_records_work_key(tmp_path):
    engine = WorkflowEngine(_runner, workspace=str(tmp_path), run_id_factory=lambda: "r1")
    engine.run(_wf(), "t", work_key="ticket-1")
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert rec["work_key"] == "ticket-1"


def test_manifest_work_key_is_none_when_not_set(tmp_path):
    engine = WorkflowEngine(_runner, workspace=str(tmp_path), run_id_factory=lambda: "r1")
    engine.run(_wf(), "t")
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert rec["work_key"] is None


def test_manifest_work_key_survives_mid_walk_update(tmp_path):
    seen = {}

    def runner(req):
        if req.node.id == "b":
            mid = run_log.read_manifest(tmp_path, "w", "r1")
            seen["work_key"] = mid.get("work_key")
        return NodeRunResponse(output=f"out {req.node.id}")

    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "b"},
        {"id": "b", "kind": "work", "next": None}]})
    WorkflowEngine(runner, workspace=str(tmp_path), run_id_factory=lambda: "r1").run(
        wf, "t", work_key="ticket-1")
    assert seen["work_key"] == "ticket-1"


# ---------------------------------------------------------------------------
# Concurrency: same-key runs serialize (one shared folder, one writer). No
# sleeps — every synchronization point below is a threading.Event or a
# thread.join(); the ONLY thing that lets run B proceed is run A's engine.run()
# actually releasing its cross_process_lock.
# ---------------------------------------------------------------------------


def _reuse_wf():
    return parse_workflow({"name": "w", "start": "producer", "nodes": [
        {"id": "producer", "kind": "work", "reuse": "if-unchanged",
         "output_schema": {"type": "object"}, "output_file": "out.json", "next": None},
    ]})


def test_concurrent_same_work_key_runs_serialize_and_second_reuses(tmp_path):
    """Run A's producer node blocks mid-dispatch (holding the work_key lock the
    whole time, since the lock is acquired before the walk starts). Run B (same
    work_key) is started only once A's node has confirmably begun — proving B's
    engine.run() call is what blocks, not test timing. Releasing A is the ONLY
    event that can let B's own dispatch proceed. If the lock did not serialize
    them, B would race in and dispatch its own producer call (no provenance
    stamped yet) while A is still blocked — this test's overlap_log would then
    show B's entries nested inside A's, and B's result would be "ok", not
    "reused"."""
    node_a_started = threading.Event()
    release_a = threading.Event()
    overlap_log: list[tuple[str, str]] = []
    log_lock = threading.Lock()

    def runner(req):
        with log_lock:
            overlap_log.append(("start", req.run_id))
        if req.run_id == "run-a":
            node_a_started.set()
            assert release_a.wait(timeout=10), "the test's own release signal was never sent"
        with log_lock:
            overlap_log.append(("end", req.run_id))
        return NodeRunResponse(output='{"x": 1}', model="m1", provider="p1", params_hash="h1")

    runner.reuse_identity = lambda node: {"model": "m1", "provider": "p1", "params_hash": "h1"}

    results: dict[str, object] = {}

    def run_a():
        engine = WorkflowEngine(runner, workspace=str(tmp_path), run_id_factory=lambda: "run-a")
        results["a"] = engine.run(_reuse_wf(), "t", work_key="shared-key")

    def run_b():
        engine = WorkflowEngine(runner, workspace=str(tmp_path), run_id_factory=lambda: "run-b")
        results["b"] = engine.run(_reuse_wf(), "t", work_key="shared-key")

    thread_a = threading.Thread(target=run_a)
    thread_b = threading.Thread(target=run_b)
    thread_a.start()
    assert node_a_started.wait(timeout=10), "run A's node never started"
    # A now holds the work_key lock (acquired before the walk) and is blocked
    # inside its own node. Starting B here means B's engine.run() call is the
    # only thing that can be waiting on the lock at this point.
    thread_b.start()
    # Confirm B is genuinely blocked (not merely "hasn't been scheduled yet")
    # BEFORE releasing A — a generous bounded join, not a sleep used to dodge a
    # race: without the lock, B's own dispatch never blocks on anything and
    # would finish this fast-path run in well under a second regardless of
    # machine speed, so is_alive() staying True here is the actual proof of
    # serialization, not a timing guess.
    thread_b.join(timeout=1.0)
    assert thread_b.is_alive(), "run B finished before run A released the lock — it never blocked"

    release_a.set()
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)

    assert not thread_a.is_alive() and not thread_b.is_alive(), "a run hung — lock never released?"
    assert results["a"].status == "completed"
    assert results["b"].status == "completed"
    assert results["a"].output_dir == results["b"].output_dir

    # The load-bearing assertion: B's producer was NEVER dispatched — it reused
    # A's freshly-stamped provenance instead, which is only possible if B's
    # engine.run() waited for A's to fully finish (stamp included) before it
    # ever looked at the ledger.
    assert overlap_log == [("start", "run-a"), ("end", "run-a")]
    assert results["b"].runs[0].status == "reused"
    assert results["b"].runs[0].origin_run_id == "run-a"


def test_concurrent_different_work_keys_do_not_block_each_other(tmp_path):
    """Sanity check on the other direction: the lock is keyed, not global — two
    DIFFERENT work_keys must run fully concurrently, neither waiting on the
    other."""
    both_started = threading.Barrier(2, timeout=10)

    def runner(req):
        both_started.wait()  # deadlocks (BrokenBarrierError) if either is still blocked on a lock
        return NodeRunResponse(output="x")

    results: dict[str, object] = {}

    def run(key, run_id):
        engine = WorkflowEngine(runner, workspace=str(tmp_path), run_id_factory=lambda: run_id)
        results[run_id] = engine.run(_wf(), "t", work_key=key)

    t1 = threading.Thread(target=run, args=("key-1", "run-1"))
    t2 = threading.Thread(target=run, args=("key-2", "run-2"))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not t1.is_alive() and not t2.is_alive()
    assert results["run-1"].status == "completed"
    assert results["run-2"].status == "completed"


# ---------------------------------------------------------------------------
# Resume (PR-K round 2 / ITEM 1): resuming a keyed run must land back in the
# SAME keyed folder even when the caller passes no work_key on the resume
# call — the manifest already recorded it at park time; ResumeState carries
# it forward from there. Without this, a paused keyed run's resume silently
# abandons whatever the pre-pause nodes wrote (a fresh empty per-run_id
# folder instead), losing artifacts and provenance mid-conversation — the
# box's primary flow (loop fire with work_key -> park -> answer()/resume).
# ---------------------------------------------------------------------------


def _parking_wf():
    return parse_workflow({"name": "w", "start": "producer", "max_visits": 3, "nodes": [
        {"id": "producer", "kind": "work", "reuse": "if-unchanged",
         "output_schema": {"type": "object"}, "output_file": "out.json", "next": "ask"},
        {"id": "ask", "kind": "work", "cases": {"READY": "do", "NEED_INFO": "__needs_input__"}},
        {"id": "do", "kind": "work", "next": None},
    ]})


def _parking_runner():
    """'ask' asks for more info on its FIRST visit (parking the run) and is
    READY on any later visit (i.e. once resumed) — the ask/answer round trip
    every parking scenario in these tests exercises."""
    calls = []
    ask_visits = {"n": 0}

    def runner(req):
        calls.append(req.node.id)
        if req.node.id == "producer":
            return NodeRunResponse(output='{"x": 1}', model="m1", provider="p1", params_hash="h1")
        if req.node.id == "ask":
            ask_visits["n"] += 1
            if ask_visits["n"] == 1:
                return NodeRunResponse(output="need more info\nNEED_INFO")
            return NodeRunResponse(output="all good\nREADY")
        return NodeRunResponse(output="done")

    runner.calls = calls
    runner.reuse_identity = lambda node: {"model": "m1", "provider": "p1", "params_hash": "h1"}
    return runner


def test_resume_of_a_keyed_run_with_no_work_key_keeps_the_same_folder(tmp_path):
    run_ids = iter(["park-run", "resume-run"])
    runner = _parking_runner()
    engine = WorkflowEngine(runner, workspace=str(tmp_path), run_id_factory=lambda: next(run_ids))

    first = engine.run(_parking_wf(), "go", work_key="ticket-1")
    assert first.status == "needs_input"
    assert first.needs_input_node == "ask"
    # needs_input results carry no output_dir (only a completed run's does) —
    # the manifest is the reliable source for the paused run's work_dir.
    park_manifest = run_log.read_manifest(tmp_path, "w", first.run_id)
    park_work_dir = Path(park_manifest["work_dir"])
    pre_pause_provenance = provenance.load(park_work_dir)
    assert "out.json" in pre_pause_provenance

    assert park_manifest["work_key"] == "ticket-1"     # recorded at park time

    resume = build_resume_state(park_manifest, "all set")
    assert resume.work_key == "ticket-1"                # carried forward from the manifest

    # The resuming caller passes NO work_key — exactly the loops answer()/
    # run_workflow resume_run_id shape today.
    second = engine.run(_parking_wf(), "all set", resume=resume)

    assert second.status == "completed"
    assert second.output_dir == str(park_work_dir)      # SAME keyed folder, not a fresh one
    assert (Path(second.output_dir) / "out.json").is_file()   # pre-pause artifact still there
    assert provenance.load(Path(second.output_dir)) == pre_pause_provenance  # provenance intact
    assert runner.calls == ["producer", "ask", "ask", "do"]   # producer never re-ran


def test_resume_of_a_keyed_run_acquires_the_keyed_lock(tmp_path, monkeypatch):
    """The keyed run-lock is acquired on resume exactly as on a fresh keyed
    run — proven by asserting cross_process_lock is invoked with the SAME
    keyed lock target for both the original park and the resume."""
    import durin.workflow.engine as engine_mod

    targets = []
    real_lock = engine_mod.cross_process_lock

    def spy_lock(target, **kw):
        targets.append(target)
        return real_lock(target, **kw)

    monkeypatch.setattr(engine_mod, "cross_process_lock", spy_lock)

    run_ids = iter(["park-run", "resume-run"])
    runner = _parking_runner()
    engine = WorkflowEngine(runner, workspace=str(tmp_path), run_id_factory=lambda: next(run_ids))

    first = engine.run(_parking_wf(), "go", work_key="ticket-1")
    manifest = run_log.read_manifest(tmp_path, "w", first.run_id)
    resume = build_resume_state(manifest, "all set")
    engine.run(_parking_wf(), "all set", resume=resume)

    assert len(targets) == 2
    assert targets[0] == targets[1]                     # same keyed lock target both times


def test_resume_of_a_non_keyed_run_is_unchanged(tmp_path):
    """No work_key ever involved: resume must keep using the plain per-run_id
    folder exactly as before this fix — a non-keyed park's manifest carries
    work_key=None, and ResumeState.work_key is None, so the default
    (run_id-based) folder computation is untouched."""
    run_ids = iter(["park-run", "resume-run"])
    runner = _parking_runner()
    engine = WorkflowEngine(runner, workspace=str(tmp_path), run_id_factory=lambda: next(run_ids))

    first = engine.run(_parking_wf(), "go")   # no work_key
    assert first.status == "needs_input"

    manifest = run_log.read_manifest(tmp_path, "w", first.run_id)
    assert manifest["work_key"] is None
    resume = build_resume_state(manifest, "all set")
    assert resume.work_key is None

    second = engine.run(_parking_wf(), "all set", resume=resume)
    assert second.status == "completed"
    assert second.run_id == first.run_id == "park-run"  # resume forces the SAME run_id
    assert second.output_dir == str(tmp_path / ".workflow" / "park-run" / "work")
    assert (tmp_path / ".workflow" / "keys").exists() is False
