"""work_key: the production entrance for the reuse gate. A caller-supplied key
picks a STABLE working folder (``.workflow/keys/<workflow>/<key>/work``) instead of
the fresh-per-run default, so a later run of the same workflow with the same key
finds the provenance an earlier run stamped — the gate is unreachable without one of
these entrances (work_key, loop re-entry/resume, or a subworkflow's work_dir_override),
since a fresh run_id otherwise always starts with an empty ledger."""

from pathlib import Path

import pytest

from durin.workflow import run_log
from durin.workflow.engine import NodeRunResponse, WorkflowEngine
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
    assert first.output_dir == str(tmp_path / ".workflow" / "keys" / "w" / "ticket-1" / "work")


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
        tmp_path / ".workflow" / "keys" / "my_workflow_" / "ticket__23124" / "work"
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
