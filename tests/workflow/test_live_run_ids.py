"""Tests for run_log.live_run_ids — the set of runs whose working folders must
survive pruning."""

from types import SimpleNamespace

from durin.workflow import run_log


def _finalize(tmp_path, name, run_id, status, needs_input_node=None):
    result = SimpleNamespace(
        run_id=run_id, status=status, final_output="", final_output_node=None,
        needs_input_node=needs_input_node, output_files=[], missing_artifacts=[],
        runs=[],
    )
    run_log.finalize_run(tmp_path, name, result, root_session_key=None,
                         started_at=0.0, finished_at=1.0)


def test_running_and_resumable_needs_input_are_live(tmp_path):
    run_log.start_run(tmp_path, "wf-a", "r-running", root_session_key=None, started_at=1.0)
    _finalize(tmp_path, "wf-a", "r-done", "completed")
    _finalize(tmp_path, "wf-b", "r-paused", "needs_input", needs_input_node="ask")
    _finalize(tmp_path, "wf-b", "r-crashed", "crashed")

    assert run_log.live_run_ids(tmp_path) == {"r-running", "r-paused"}


def test_needs_input_without_reentry_node_is_not_live(tmp_path):
    """A needs_input run with no re-entry node cannot be resumed (the resume
    endpoints reject it), so its working folder is not owed protection —
    the same rule prune_manifests applies to the manifest itself."""
    _finalize(tmp_path, "wf", "r-ghost", "needs_input", needs_input_node=None)
    assert run_log.live_run_ids(tmp_path) == set()


def test_no_manifests_means_no_live_runs(tmp_path):
    assert run_log.live_run_ids(tmp_path) == set()


def test_a_corrupt_manifest_is_skipped_not_fatal(tmp_path):
    run_log.start_run(tmp_path, "wf", "r-ok", root_session_key=None, started_at=1.0)
    bad = run_log.runs_root(tmp_path) / "wf" / "broken.json"
    bad.write_text("{not json", encoding="utf-8")

    assert run_log.live_run_ids(tmp_path) == {"r-ok"}


def test_engine_pruning_spares_a_live_foreign_run(tmp_path):
    """The wiring pin: a run started by another engine (its manifest says
    running) keeps its working folder even when this engine's prune pass
    would otherwise evict it as the oldest folder on disk."""
    import os
    import time

    from durin.workflow.artifacts import artifact_dir
    from durin.workflow.engine import NodeRunResponse, WorkflowEngine
    from durin.workflow.spec import parse_workflow

    # A foreign run, mid-flight: manifest running + an old, mtime-frozen folder.
    run_log.start_run(tmp_path, "other-wf", "foreign-live", root_session_key=None, started_at=1.0)
    live_dir = artifact_dir(tmp_path, "foreign-live", "n", 1).parent.parent
    old = time.time() - 7200
    os.utime(live_dir, (old, old))
    # Two terminal stragglers: with prune_keep=1 the newer one consumes the
    # keep slot and the older one is fair game.
    _finalize(tmp_path, "other-wf", "foreign-done", "completed")
    dead_dir = artifact_dir(tmp_path, "foreign-done", "n", 1).parent.parent
    os.utime(dead_dir, (old + 1, old + 1))
    _finalize(tmp_path, "other-wf", "foreign-done2", "completed")
    kept_dir = artifact_dir(tmp_path, "foreign-done2", "n", 1).parent.parent
    os.utime(kept_dir, (old + 2, old + 2))

    wf = parse_workflow({"name": "wf", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": None},
    ]})
    engine = WorkflowEngine(
        node_runner=lambda req: NodeRunResponse(output="ok", session_key=None, messages=[]),
        workspace=str(tmp_path), prune_keep=1,
    )
    engine.run(wf, "t")

    assert live_dir.is_dir(), "a live run's folder was pruned out from under it"
    assert kept_dir.is_dir(), "the newest terminal folder holds the keep slot"
    assert not dead_dir.is_dir(), "the older terminal straggler should have been pruned"
