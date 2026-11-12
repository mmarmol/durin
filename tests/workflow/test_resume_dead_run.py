"""A run whose process died is resumed at the step it was in, with the input
that step had, and what had finished does not run again."""
import json
import shutil
from pathlib import Path

import pytest

from durin.utils.file_lock import cross_process_lock
from durin.workflow import run_log, workspace_fork
from durin.workflow.diagnostics import compute_diagnostics
from durin.workflow.engine import build_resume_state, resumable_kind, resume_refusal
from durin.workflow.spec import parse_workflow
from tests.workflow import _crash_driver as driver
from tests.workflow.crash_harness import (
    HANG_IN_APPLY_ENV,
    engine,
    marks,
    run_to_end,
    run_until_killed,
)
from tests.workflow.telemetry_capture import (
    assert_clean,
    payloads,
    recorded,
)
from tests.workflow.telemetry_capture import telemetry_dir_fixture as _telemetry_dir  # noqa: F401

CHAIN = {"name": "w", "start": "a", "nodes": [
    {"id": "a", "kind": "work", "tools": "default", "next": "b"},
    {"id": "b", "kind": "script", "command": "cat; echo ' via b'", "next": "c"},
    {"id": "c", "kind": "work", "next": None}]}


def _resume(dead, definition=CHAIN, rec=None):
    if rec is None:
        rec = run_log.mark_dead_run(dead.workspace, definition["name"], "r1")
    wf = parse_workflow(definition)
    assert resume_refusal(rec, workspace=dead.workspace, workflow=wf) is None
    return engine(dead.workspace).run(
        wf, "t", resume=build_resume_state(rec, "", workspace=dead.workspace))


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _given(workspace: Path, step: str) -> list:
    """What ``step`` received at each start: an agent step's task, shared
    conversation and edge text; a script step's stdin, byte for byte."""
    marks_folder = driver.marks_dir(workspace)
    if (marks_folder / f"{step}.seen").is_file():
        return driver.seen(marks_folder, step)
    starts = len(_lines(marks_folder / step))
    return [(marks_folder / f"{step}.stdin.{n}").read_bytes() for n in range(1, starts + 1)]


def _attempts(workspace: Path, name: str, step: str) -> list[tuple[int, bool]]:
    """The walk's own rows of ``step`` over every walk of run r1: (attempt, cut)."""
    rec = run_log.read_manifest(workspace, name, "r1")
    return [(r["iteration"], bool(r.get("cut"))) for r in rec["history"] + rec["runs"]
            if r["node_id"] == step and r.get("branch_id") is None
            and r.get("worker_index") is None]


@pytest.mark.parametrize("stop_in,again", [("a", {"a": 2, "b": 1, "c": 1}),
                                           ("b", {"a": 1, "b": 2, "c": 1}),
                                           ("c", {"a": 1, "b": 1, "c": 2})])
def test_only_the_step_that_was_cut_runs_again(tmp_path, stop_in, again):
    whole, _ = run_to_end(tmp_path / "whole", CHAIN)
    dead = run_until_killed(tmp_path / "cut", CHAIN, stop_in=stop_in)
    done = _resume(dead)
    assert done.status == "completed"
    assert done.final_output == whole["final_output"]
    assert marks(dead.workspace) == again
    assert _attempts(dead.workspace, "w", stop_in) == [(1, True), (2, False)]
    first, second = _given(dead.workspace, stop_in)
    assert second == first          # the step that was cut got the same input again


def test_the_attempt_numbers_never_repeat(tmp_path):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="c")
    _resume(dead)
    rec = run_log.read_manifest(dead.workspace, "w", "r1")
    rows = [(r["node_id"], r["iteration"]) for r in rec["history"] + rec["runs"]]
    assert len(rows) == len(set(rows)) and ("c", 1) in rows and ("c", 2) in rows


def test_a_run_killed_twice_is_resumed_twice(tmp_path):
    run_until_killed(tmp_path, CHAIN, stop_in="c")
    # The resume itself runs in a child process and is killed in "c" again.
    dead = run_until_killed(tmp_path, CHAIN, stop_in="c", env={driver.RESUME_ENV: "1"})
    assert dead.record["status"] == "running"
    done = _resume(dead)
    assert done.status == "completed"
    assert marks(dead.workspace) == {"a": 1, "b": 1, "c": 3}


def test_a_parallel_step_that_was_cut_runs_all_its_branches_again(tmp_path):
    definition = {"name": "w", "start": "pre", "nodes": [
        {"id": "pre", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "branches": ["left", "right"],
         "reconcile": "union", "next": "after"},
        {"id": "left", "kind": "work", "tools": "default"},
        {"id": "right", "kind": "work", "tools": "default"},
        {"id": "after", "kind": "work", "next": None}]}
    dead = run_until_killed(tmp_path, definition, stop_in="right",
                            env={driver.HANG_AFTER_ENV: "left"})
    done = _resume(dead, definition)
    assert done.status == "completed"
    m = marks(dead.workspace)
    assert (m["left"], m["right"], m["pre"], m["after"]) == (2, 2, 1, 1)
    folder = Path(run_log.read_manifest(dead.workspace, "w", "r1")["work_dir"])
    assert (folder / "left.txt").is_file() and (folder / "right.txt").is_file()
    assert _attempts(dead.workspace, "w", "fan") == [(1, True), (2, False)]


def _writing_fan(reconcile):
    fan = {"id": "fan", "kind": "parallel", "branches": ["left", "right"],
           "reconcile": reconcile, "next": "after"}
    if reconcile == "choose":
        fan["criteria"] = "any"
    return {"name": "w", "start": "pre", "nodes": [
        {"id": "pre", "kind": "work", "next": "fan"}, fan,
        {"id": "left", "kind": "work", "tools": "default"},
        {"id": "right", "kind": "work", "tools": "default"},
        {"id": "after", "kind": "work", "next": None}]}


def _copies(dead, run_id="r1", name="w"):
    """The run's branch-copies folder for ``run_id`` (it may not exist)."""
    work = Path(run_log.read_manifest(dead.workspace, name, "r1")["work_dir"])
    return workspace_fork.branches_root(work) / run_id


@pytest.mark.parametrize("reconcile", ["union", "choose"])
def test_a_killed_parallel_step_leaves_its_copies_under_the_run_and_the_conversion_removes_them(
        tmp_path, reconcile):
    definition = _writing_fan(reconcile)
    dead = run_until_killed(tmp_path, definition, stop_in="right",
                            env={driver.HANG_AFTER_ENV: "left"})
    copies = _copies(dead)
    # The dead process's copies are in its run's folder, none in its temporary directory.
    assert not list((tmp_path / "tmp").glob("durin_wf_branch_*"))
    assert list((tmp_path / "tmp").iterdir()) == []
    assert len(list(copies.iterdir())) == 2 and all(p.name.startswith("b-") for p in copies.iterdir())

    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    assert rec["status"] == "crashed"
    assert not copies.exists()
    assert [p for p in copies.parent.iterdir()] == []        # no retired leftover either

    done = _resume(dead, definition, rec)
    assert done.status == "completed"
    assert list(copies.parent.iterdir()) == []


def test_a_directory_made_for_the_run_after_the_conversion_survives_the_delayed_delete(
        tmp_path, monkeypatch):
    definition = _writing_fan("union")
    dead = run_until_killed(tmp_path, definition, stop_in="right",
                            env={driver.HANG_AFTER_ENV: "left"})
    copies = _copies(dead)
    real_cleanup = workspace_fork.cleanup
    removed: list[Path] = []

    def after_the_lock_is_released(path):
        # A resume claimed the run and its step made new copies in the time between
        # the conversion and this delayed delete.
        assert path.name.startswith(".dead-r1-") and path != copies
        (copies / "left-new").mkdir(parents=True)
        (copies / "left-new" / "x.txt").write_text("fresh")
        removed.append(path)
        real_cleanup(path)

    monkeypatch.setattr(workspace_fork, "cleanup", after_the_lock_is_released)
    assert run_log.mark_dead_run(dead.workspace, "w", "r1")["status"] == "crashed"

    assert len(removed) == 1 and not removed[0].exists()
    assert (copies / "left-new" / "x.txt").read_text() == "fresh"


def test_the_copies_are_renamed_while_the_lock_is_held_and_the_record_already_reads_crashed(
        tmp_path, monkeypatch):
    import threading

    definition = _writing_fan("union")
    dead = run_until_killed(tmp_path, definition, stop_in="right",
                            env={driver.HANG_AFTER_ENV: "left"})
    real_retire = workspace_fork.retire_branches
    seen: dict = {}

    def at_the_rename(work_dir, run_id):
        def try_the_lock():
            try:
                with cross_process_lock(run_log.run_lock_target(dead.workspace, "w", "r1"),
                                        timeout=0.3):
                    seen["lock_free"] = True
            except TimeoutError:
                seen["lock_free"] = False
        # A lock is held per thread, so another thread tells whether it is held at all.
        other = threading.Thread(target=try_the_lock)
        other.start()
        other.join()
        seen["status"] = run_log.read_manifest(dead.workspace, "w", "r1")["status"]
        return real_retire(work_dir, run_id)

    monkeypatch.setattr(workspace_fork, "retire_branches", at_the_rename)
    assert run_log.mark_dead_run(dead.workspace, "w", "r1")["status"] == "crashed"
    assert seen == {"lock_free": False, "status": "crashed"}


def test_copies_stay_while_a_script_of_the_dead_run_may_still_run(tmp_path):
    """A script record the dead run left means a script may still be writing in
    a copy: the conversion leaves the copies, and the step removes them when it
    is entered again."""
    definition = _writing_fan("union")
    dead = run_until_killed(tmp_path, definition, stop_in="right",
                            env={driver.HANG_AFTER_ENV: "left"})
    copies = _copies(dead)
    folder = run_log._record_path(dead.workspace, "w", "r1").parent
    run_log.record_script(folder, "r1", pid=2**22 + 7, started="never", node_id="left")

    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")

    assert rec["status"] == "crashed"
    assert len(list(copies.iterdir())) == 2 and all(p.name.startswith("b-") for p in copies.iterdir())
    done = _resume(dead, definition, rec)
    assert done.status == "completed"
    assert list(copies.parent.iterdir()) == []


def test_a_dead_sub_workflow_runs_copies_are_removed_by_its_own_conversion(tmp_path):
    parent = {"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "sub"},
        {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": "z"},
        {"id": "z", "kind": "work", "next": None}]}
    child = {"name": "child", "start": "pre", "nodes": [
        {"id": "pre", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "branches": ["left", "right"],
         "reconcile": "union", "next": None},
        {"id": "left", "kind": "work", "tools": "default"},
        {"id": "right", "kind": "work", "tools": "default"}]}
    flows = tmp_path / "ws" / "workflows"
    flows.mkdir(parents=True)
    (flows / "child.json").write_text(json.dumps(child), encoding="utf-8")

    dead = run_until_killed(tmp_path, parent, stop_in="right",
                            env={driver.HANG_AFTER_ENV: "left"})
    [kid] = run_log.list_runs(dead.workspace, "child")
    kid_record = run_log.read_manifest(dead.workspace, "child", kid["run_id"])
    work = Path(run_log.read_manifest(dead.workspace, "w", "r1")["work_dir"])
    assert kid_record["work_dir"] == str(work)          # the child works in its parent's folder
    kid_copies = workspace_fork.branches_root(work) / kid["run_id"]
    assert kid_copies.is_dir() and any(kid_copies.iterdir())

    assert run_log.mark_dead_run(dead.workspace, "w", "r1")["status"] == "crashed"

    assert run_log.read_manifest(dead.workspace, "child", kid["run_id"])["status"] == "crashed"
    assert not kid_copies.exists()
    assert not any(p.name.startswith(".dead-") for p in kid_copies.parent.iterdir())


@pytest.mark.parametrize("reconcile", ["read", "choose", "union"])
def test_each_kind_of_static_parallel_step_runs_whole_again(tmp_path, reconcile):
    fan = {"id": "fan", "kind": "parallel", "branches": ["left", "right"],
           "reconcile": reconcile, "next": "after"}
    if reconcile == "choose":
        fan["criteria"] = "any"
    definition = {"name": "w", "start": "pre", "nodes": [
        {"id": "pre", "kind": "work", "next": "fan"}, fan,
        {"id": "left", "kind": "work"},
        {"id": "right", "kind": "work"},
        {"id": "after", "kind": "work", "next": None}]}
    dead = run_until_killed(tmp_path, definition, stop_in="right",
                            env={driver.HANG_AFTER_ENV: "left"})
    done = _resume(dead, definition)
    assert done.status == "completed"
    m = marks(dead.workspace)
    assert (m["left"], m["right"], m["pre"], m["after"]) == (2, 2, 1, 1)


LOGS = {"name": "w", "start": "seed", "nodes": [
    {"id": "seed", "kind": "script", "command": "echo seed > a.log; echo seed > b.log",
     "next": "fan"},
    {"id": "fan", "kind": "parallel", "branches": ["left", "right"], "reconcile": "union",
     "next": "after"},
    {"id": "left", "kind": "script", "command": "echo a >> a.log"},
    {"id": "right", "kind": "script", "command": "echo b >> b.log"},
    {"id": "after", "kind": "script", "command": "cat a.log b.log", "next": None}]}


def test_a_union_killed_while_writing_back_resumes_from_the_folder_before_it(tmp_path):
    whole, _ = run_to_end(tmp_path / "whole", LOGS)
    whole_dir = Path(whole["work_dir"])
    dead = run_until_killed(tmp_path / "cut", LOGS, stop_in=None,
                            env={HANG_IN_APPLY_ENV: "1"})
    folder = Path(dead.record["work_dir"])
    # Killed after the first real file of the write-back was written.
    assert _lines(folder / "a.log") == ["seed", "a"]
    assert _lines(folder / "b.log") == ["seed"]
    assert (workspace_fork.pending_dir(folder) / "journal.json").is_file()

    done = _resume(dead, LOGS)
    assert done.status == "completed"
    assert _lines(folder / "a.log") == ["seed", "a"] == _lines(whole_dir / "a.log")
    assert _lines(folder / "b.log") == ["seed", "b"] == _lines(whole_dir / "b.log")
    assert not workspace_fork.pending_dir(folder).exists()


def test_a_sub_workflow_killed_while_writing_back_is_undone_before_its_first_step(tmp_path):
    # The child writes into its caller's folder. Its first step records what the
    # folder holds, outside the folder; the union's own entry would also undo the
    # cut write-back, but only after that first step has run.
    seen = tmp_path / "seen.log"
    parent = {"name": "w", "start": "seed", "nodes": [
        {"id": "seed", "kind": "script", "command": "echo seed > a.log; echo seed > b.log",
         "next": "sub"},
        {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": "after"},
        {"id": "after", "kind": "script", "command": "cat a.log b.log", "next": None}]}
    child = {"name": "child", "start": "look", "nodes": [
        {"id": "look", "kind": "script",
         "command": f"cat a.log b.log >> '{seen}'; echo -- >> '{seen}'", "next": "fan"},
        {"id": "fan", "kind": "parallel", "branches": ["left", "right"], "reconcile": "union",
         "next": "end"},
        {"id": "left", "kind": "script", "command": "echo a >> a.log"},
        {"id": "right", "kind": "script", "command": "echo b >> b.log"},
        {"id": "end", "kind": "script", "command": "true", "next": None}]}
    flows = tmp_path / "ws" / "workflows"
    flows.mkdir(parents=True)
    (flows / "child.json").write_text(json.dumps(child), encoding="utf-8")

    dead = run_until_killed(tmp_path, parent, stop_in=None, env={HANG_IN_APPLY_ENV: "1"})
    folder = Path(dead.record["work_dir"])
    [first_child] = run_log.list_runs(dead.workspace, "child")
    child_record = run_log.read_manifest(dead.workspace, "child", first_child["run_id"])
    assert Path(child_record["work_dir"]) == folder
    # Killed after the first real file of the child's write-back was written.
    assert _lines(folder / "a.log") == ["seed", "a"]
    assert _lines(folder / "b.log") == ["seed"]
    assert (workspace_fork.pending_dir(folder) / "journal.json").is_file()
    assert _lines(seen) == ["seed", "seed", "--"]

    assert run_log.mark_dead_run(dead.workspace, "child", first_child["run_id"]) is not None
    done = _resume(dead, parent)
    assert done.status == "completed", done.final_output
    # The child's first step, run again, saw the folder as it was before the write-back.
    assert _lines(seen) == ["seed", "seed", "--", "seed", "seed", "--"]
    assert _lines(folder / "a.log") == ["seed", "a"]
    assert _lines(folder / "b.log") == ["seed", "b"]
    assert not workspace_fork.pending_dir(folder).exists()


def test_a_fan_out_that_was_cut_runs_every_worker_again(tmp_path):
    definition = {"name": "w", "start": "ids", "nodes": [
        {"id": "ids", "kind": "script", "command": """echo '["one","two","three"]'""",
         "next": "fan"},
        {"id": "fan", "kind": "parallel", "worker": "w", "list_from": "ids", "next": "after"},
        {"id": "w", "kind": "work"},
        {"id": "after", "kind": "work", "next": None}]}
    dead = run_until_killed(tmp_path, definition, stop_in=None,
                            env={driver.HANG_TASK_ENV: "three",
                                 driver.HANG_AFTER_ENV: "w=one,w=two"})
    done = _resume(dead, definition)
    assert done.status == "completed"
    m = marks(dead.workspace)
    assert (m["ids"], m["w"], m["after"]) == (1, 6, 1)
    assert _attempts(dead.workspace, "w", "fan") == [(1, True), (2, False)]
    tasks = [s["task"] for s in _given(dead.workspace, "w")]
    assert sorted(tasks[:3]) == sorted(tasks[3:]) == ["one", "three", "two"]


def test_a_sub_workflow_that_was_cut_starts_its_child_again(tmp_path):
    parent = {"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "sub"},
        {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": "z"},
        {"id": "z", "kind": "work", "next": None}]}
    child = {"name": "child", "start": "x", "nodes": [
        {"id": "x", "kind": "work", "next": "y"},
        {"id": "y", "kind": "work", "next": None}]}
    flows = tmp_path / "ws" / "workflows"
    flows.mkdir(parents=True)
    (flows / "child.json").write_text(json.dumps(child), encoding="utf-8")

    dead = run_until_killed(tmp_path, parent, stop_in="y")
    [first_child] = run_log.list_runs(dead.workspace, "child")
    # The gateway's sweep finds the dead child too.
    assert run_log.mark_dead_run(dead.workspace, "child", first_child["run_id"]) is not None
    done = _resume(dead, parent)

    assert done.status == "completed"
    m = marks(dead.workspace)
    assert (m["x"], m["y"], m["a"], m["z"]) == (2, 2, 1, 1)
    assert _attempts(dead.workspace, "w", "sub") == [(1, True), (2, False)]
    child_record = run_log.read_manifest(dead.workspace, "child", first_child["run_id"])
    assert child_record["status"] == "crashed"
    assert "'r1'" in resume_refusal(child_record)


def test_a_shared_step_that_was_cut_sees_the_conversation_again(tmp_path):
    definition = {"name": "w", "start": "s1", "nodes": [
        {"id": "s1", "kind": "work", "context": "shared", "next": "s2"},
        {"id": "s2", "kind": "work", "context": "shared", "next": "s3"},
        {"id": "s3", "kind": "work", "context": "shared", "next": None}]}
    dead = run_until_killed(tmp_path, definition, stop_in="s3")
    done = _resume(dead, definition)
    assert done.status == "completed"
    first, second = _lines(driver.marks_dir(dead.workspace) / "s3.seen")
    assert second == first
    assert json.loads(first)["shared"]   # the conversation was not empty the first time


def test_a_persistent_step_that_was_cut_continues_its_conversation(tmp_path):
    definition = {"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "session": "persistent", "next": None}]}
    dead = run_until_killed(tmp_path, definition, stop_in="a")
    done = _resume(dead, definition)
    assert done.status == "completed"
    assert marks(dead.workspace)["a"] == 2
    assert _lines(driver.marks_dir(dead.workspace) / "a.iter") == ["1", "2"]


def test_a_dead_run_from_before_positions_is_refused(tmp_path):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="b")
    path = run_log._record_path(dead.workspace, "w", "r1")
    rec = json.loads(path.read_text()); rec.pop("position"); path.write_text(json.dumps(rec))
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    before = path.read_bytes()
    assert "before a run kept the step" in resume_refusal(rec, workspace=dead.workspace)
    assert resumable_kind(rec) is None
    assert path.read_bytes() == before


def test_a_step_removed_from_the_definition_is_refused(tmp_path):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="c")
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    smaller = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "tools": "default", "next": "b"},
        {"id": "b", "kind": "script", "command": "cat; echo ' via b'", "next": None}]})
    assert "'c' is no longer in the workflow" in resume_refusal(rec, workspace=dead.workspace, workflow=smaller)


def test_a_pruned_folder_is_refused(tmp_path):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="c")
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    shutil.rmtree(dead.workspace / ".workflow" / "r1")
    assert "pruned" in resume_refusal(rec, workspace=dead.workspace, workflow=parse_workflow(CHAIN))


def test_a_later_run_on_the_same_key_takes_over(tmp_path):
    keyed = {driver.WORK_KEY_ENV: "ticket-7"}
    dead = run_until_killed(tmp_path, CHAIN, stop_in="c", env=keyed)
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    later, _ = run_to_end(tmp_path, CHAIN, env={**keyed, driver.RUN_ID_ENV: "r2"})
    assert later["status"] == "completed" and later["work_key"] == "ticket-7"
    refusal = resume_refusal(rec, workspace=dead.workspace, workflow=parse_workflow(CHAIN))
    assert "has taken over its folder" in refusal and "'r2'" in refusal


def test_a_cut_row_is_not_evidence_against_the_step(tmp_path):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="b")
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    [cut] = [r for r in rec["runs"] if r.get("cut")]
    assert cut["node_id"] == "b" and cut["status"] == "node_failed"

    # The improve pass's failure evidence: neither counted nor sampled.
    found = compute_diagnostics([rec])
    assert "b" not in found.script_failures and "b" not in found.failure_samples
    # A run that a stop ended while the step was cut is one the improve pass reads.
    found = compute_diagnostics([{**rec, "status": "cancelled"}])
    assert "b" not in found.script_failures and "b" not in found.failure_samples

    # Durations: only completed runs are read, and a cut row never counts, even
    # one carrying a duration.
    timed = {**rec, "run_id": "r9", "status": "completed",
             "runs": [{**r, "duration_s": 99.0} if r.get("cut") else {**r, "duration_s": 1.0}
                      for r in rec["runs"]]}
    run_log._write_json(run_log._record_path(dead.workspace, "w", "r9"), timed)
    typical = run_log.typical_node_durations(dead.workspace, "w")
    assert typical == {"a": 1.0}


def test_a_dead_run_that_died_between_steps_is_resumed_at_the_next_one(tmp_path):
    # The process died after "b" finished and the record moved to "c", before "c"
    # started: no cut row names "c", and it is still the step to enter.
    dead = run_until_killed(tmp_path, CHAIN, stop_in="c")
    path = run_log._record_path(dead.workspace, "w", "r1")
    rec = json.loads(path.read_text())
    rec["active_node"] = None
    path.write_text(json.dumps(rec))
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    assert not any(r.get("cut") for r in rec["runs"])
    assert resumable_kind(rec) == "dead"
    done = _resume(dead, rec=rec)
    assert done.status == "completed"
    assert marks(dead.workspace) == {"a": 1, "b": 1, "c": 2}


def test_a_dead_run_whose_paused_resume_died_is_entered_as_a_dead_run(tmp_path):
    # A paused run was claimed by a resume whose process then died before the
    # engine wrote: the record still names the question, but it is a dead run,
    # entered again at its position with the stored input, not with answers.
    dead = run_until_killed(tmp_path, CHAIN, stop_in="c")
    path = run_log._record_path(dead.workspace, "w", "r1")
    rec = json.loads(path.read_text())
    rec.update(needs_input_node="a", ask_kind="question", final_output="which env?")
    path.write_text(json.dumps(rec))
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    assert resumable_kind(rec) == "dead"
    state = build_resume_state(rec, "prod", workspace=dead.workspace)
    assert state.start_at == "c"
    assert "via b" in state.upstream and "prod" not in state.upstream
    assert state.upstream == rec["resume_upstream"]


def test_resume_refusal_keeps_its_old_signature_for_paused_and_failed_runs():
    paused = {"run_id": "p", "status": "needs_input", "needs_input_node": "a", "runs": []}
    failed = {"run_id": "f", "status": "aborted", "failed_node": "a",
              "runs": [{"node_id": "a", "status": "node_failed"}]}
    assert resume_refusal(paused) is None and resume_refusal(failed) is None
    assert resumable_kind(paused) == "paused" and resumable_kind(failed) == "failed"


@pytest.mark.parametrize("position,failed,reason", [
    (None, None, "cannot be entered again"),
    ({"node": "c", "seq": 3}, None, "cannot be entered again"),
    ({"node": "c", "seq": 3}, "b", "cannot be entered again"),
])
def test_a_crashed_record_whose_step_cannot_be_entered_is_refused(position, failed, reason):
    rec = {"run_id": "r", "status": "crashed", "position": position, "runs": []}
    if failed:
        rec["failed_node"] = failed
    assert resumable_kind(rec) is None
    assert reason in resume_refusal(rec)


_AUTOMATION_REFUSAL = (
    "it was started by an automation, and a run an automation started is not resumed by "
    "hand: the automation does not resume it, and its next run starts anew.")


def test_a_dead_run_an_automation_started_is_refused_and_no_other_run_is():
    dead = {"run_id": "r", "status": "crashed", "position": {"node": "a", "seq": 1},
            "failed_node": "a", "runs": []}
    assert resume_refusal(dead) is None
    assert resume_refusal({**dead, "root_session_key": "s"}) is None
    assert resume_refusal({**dead, "root_session_key": "automation:nightly"}) == (
        _AUTOMATION_REFUSAL)
    # A paused run's answer and a failed step's retry are not dead-run resumes.
    paused = {"run_id": "p", "status": "needs_input", "needs_input_node": "a", "runs": [],
              "root_session_key": "automation:nightly"}
    failed = {"run_id": "f", "status": "aborted", "failed_node": "a",
              "runs": [{"node_id": "a", "status": "node_failed"}],
              "root_session_key": "automation:nightly"}
    assert resume_refusal(paused) is None and resume_refusal(failed) is None


_INCOMPLETE = ("its record is incomplete: it was rewritten without what the run was "
               "started with.")


def _from_nothing(tmp_path, monkeypatch):
    """A run whose record the walk rewrote with nothing to carry from (its one
    read failed), then whose process died: converted, as a sweep would."""
    from durin.workflow import resume_store
    from durin.workflow.result import WorkflowResult

    run_log.start_run(tmp_path, "w", "r1", root_session_key="automation:nightly",
                      started_at=1.0, position={"node": "a", "seq": 1})
    resume_store.save_input(resume_store.store_dir(tmp_path, "r1"), 2, "b", "in")
    real = run_log.read_manifest
    monkeypatch.setattr(run_log, "read_manifest", lambda *_a, **_kw: None)
    run_log.update_run(tmp_path, "w", "r1",
                       WorkflowResult(status="running", final_output=None, runs=[]),
                       position={"node": "b", "seq": 2})
    monkeypatch.setattr(run_log, "read_manifest", real)
    path = run_log._record_path(tmp_path, "w", "r1")
    rec = json.loads(path.read_text(encoding="utf-8"))
    run_log._write_json(path, {**rec, "owner": {"pid": 2**22 + 4242, "started": "never"}})
    return run_log.mark_dead_run(tmp_path, "w", "r1")


def test_a_dead_run_whose_record_was_rewritten_from_nothing_is_refused(tmp_path, monkeypatch):
    rec = _from_nothing(tmp_path, monkeypatch)
    assert rec["status"] == "crashed" and rec["failed_node"] == "b"
    assert rec["root_session_key"] is None        # the automation's root was lost
    assert resume_refusal(rec, workspace=tmp_path, workflow=parse_workflow(CHAIN)) == _INCOMPLETE
    # A failed step's retry and a paused run's answer are not dead-run resumes.
    assert resume_refusal({**rec, "status": "aborted",
                           "runs": [{"node_id": "b", "status": "node_failed"}]}) is None
    assert resume_refusal({**rec, "status": "needs_input", "needs_input_node": "b"}) is None


def test_a_normal_dead_run_and_a_released_record_are_judged_as_before(tmp_path):
    from durin.workflow import resume_store

    resume_store.save_input(resume_store.store_dir(tmp_path, "r1"), 1, "a", "in")
    run_log.start_run(tmp_path, "w", "r1", root_session_key="s", started_at=1.0,
                      position={"node": "a", "seq": 1})
    path = run_log._record_path(tmp_path, "w", "r1")
    rec = json.loads(path.read_text(encoding="utf-8"))
    run_log._write_json(path, {**rec, "owner": {"pid": 2**22 + 4242, "started": "never"}})
    dead = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert "incomplete" not in dead
    assert resume_refusal(dead, workspace=tmp_path, workflow=parse_workflow(CHAIN)) is None
    # The shape the released version's start_run and update_run write: no
    # position, no incomplete mark.
    released = {"schema": rec["schema"], "run_id": "old", "workflow": "w",
                "status": "crashed", "root_session_key": "s", "started_at": 1.0,
                "ts": 1.0, "task": None, "parent_run_id": None, "work_dir": None,
                "work_key": None, "typical_s": {}, "typical_total_s": None,
                "spec_hash": None, "durin_version": None, "resumed": False,
                "earlier_nodes": [], "resume_inputs": None, "active_node": None, "runs": []}
    assert resume_refusal(released) == (
        "it was recorded before a run kept the step it was in. Start a new run instead.")


def test_a_sub_workflow_run_names_the_surface_of_its_caller(tmp_path):
    from durin.workflow.engine import WorkflowEngine
    from durin.workflow.subworkflow import SubworkflowRunner

    flows = tmp_path / "workflows"
    flows.mkdir()
    (flows / "child.json").write_text(json.dumps({"name": "child", "start": "x", "nodes": [
        {"id": "x", "kind": "work", "next": None}]}), encoding="utf-8")
    agent = driver.StandInAgent(tmp_path)
    result = WorkflowEngine(
        agent, workspace=str(tmp_path), run_id_factory=lambda: "p1", launch_surface="agent",
        subworkflow_runner=SubworkflowRunner(tmp_path, agent, launch_surface="agent"),
    ).run(parse_workflow({"name": "w", "start": "sub", "nodes": [
        {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": None}]}), "t")
    assert result.status == "completed"
    [child] = run_log.list_runs(tmp_path, "child")
    assert run_log.read_manifest(tmp_path, "w", "p1")["surface"] == "agent"
    assert run_log.read_manifest(tmp_path, "child", child["run_id"])["surface"] == "agent"


def _flow(folder: Path, name: str, nodes: list[dict]) -> dict:
    definition = {"name": name, "start": nodes[0]["id"], "nodes": nodes}
    (folder / f"{name}.json").write_text(json.dumps(definition), encoding="utf-8")
    return definition


def test_the_mcp_refusal_follows_sub_workflows_once_each_and_only_for_a_dead_run(tmp_path):
    from durin.workflow.engine import mcp_tools_refusal

    flows = tmp_path / "workflows"
    flows.mkdir()
    # p -> loop (names itself and p) -> deep (has an mcps step); gone is missing.
    parent = _flow(flows, "p", [
        {"id": "s1", "kind": "subworkflow", "workflow": "loop", "next": "s2"},
        {"id": "s2", "kind": "subworkflow", "workflow": "gone", "next": None}])
    _flow(flows, "loop", [
        {"id": "again", "kind": "subworkflow", "workflow": "loop", "next": "up"},
        {"id": "up", "kind": "subworkflow", "workflow": "p", "next": "down"},
        {"id": "down", "kind": "subworkflow", "workflow": "deep", "next": None}])
    _flow(flows, "deep", [{"id": "t", "kind": "work", "mcps": ["tracker"], "next": None}])
    dead = {"run_id": "r", "status": "crashed", "surface": "agent",
            "position": {"node": "s1", "seq": 1}, "failed_node": "s1"}
    wf = parse_workflow(parent)
    assert "only a run resumed by the agent" in mcp_tools_refusal(dead, wf, workspace=tmp_path)
    # Not for a run another surface launched, a record without a surface, or a run
    # that is paused or failed.
    assert mcp_tools_refusal({**dead, "surface": "service"}, wf, workspace=tmp_path) is None
    assert mcp_tools_refusal({k: v for k, v in dead.items() if k != "surface"}, wf,
                             workspace=tmp_path) is None
    paused = {**dead, "status": "needs_input", "needs_input_node": "s1"}
    failed = {**dead, "status": "aborted"}
    assert mcp_tools_refusal(paused, wf, workspace=tmp_path) is None
    assert mcp_tools_refusal(failed, wf, workspace=tmp_path) is None
    # Without the mcps step anywhere, the cycle and the missing workflow end the walk.
    _flow(flows, "deep", [{"id": "t", "kind": "work", "next": None}])
    assert mcp_tools_refusal(dead, wf, workspace=tmp_path) is None


# The input a re-entered step receives is the whole text, not the record's capped
# copies (resume_upstream, resume_inputs).

def _pad(n: int) -> str:
    """A shell expression printing ``n`` x characters."""
    return f"head -c {n} /dev/zero | tr '\\0' x"


def test_a_cut_step_gets_its_whole_input_beyond_the_records_cap(tmp_path):
    size = run_log.RESUME_UPSTREAM_MAX_CHARS + 4_000
    definition = {"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "c"},
        {"id": "c", "kind": "work", "next": None}]}
    dead = run_until_killed(tmp_path, definition, stop_in="c",
                            env={driver.OUTPUT_CHARS_ENV: f"a={size}"})
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    assert len(rec["resume_upstream"]) == run_log.RESUME_UPSTREAM_MAX_CHARS
    assert _resume(dead, definition, rec=rec).status == "completed"
    first, second = _given(dead.workspace, "c")
    assert len(first["upstream"]) == size and second == first


def test_a_cut_step_reading_a_long_source_by_name_gets_the_same_composed_input(tmp_path):
    size = run_log.RESUME_INPUT_MAX_CHARS + 2_000
    definition = {"name": "w", "start": "big", "nodes": [
        {"id": "big", "kind": "work", "next": "mid"},
        {"id": "mid", "kind": "work", "next": "c"},
        {"id": "c", "kind": "work", "inputs_from": ["big"], "next": None}]}
    dead = run_until_killed(tmp_path, definition, stop_in="c",
                            env={driver.OUTPUT_CHARS_ENV: f"big={size}"})
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    assert len(rec["resume_inputs"]["big"]) == run_log.RESUME_INPUT_MAX_CHARS
    assert _resume(dead, definition, rec=rec).status == "completed"
    first, second = _given(dead.workspace, "c")
    assert "big-out".ljust(size, "x") in first["upstream"] and second == first


def test_a_cut_fan_out_reading_a_long_list_starts_the_same_workers(tmp_path):
    long_item = f'three$({_pad(run_log.RESUME_INPUT_MAX_CHARS)})'
    definition = {"name": "w", "start": "ids", "nodes": [
        {"id": "ids", "kind": "script", "command": f'echo "[\\"one\\",\\"two\\",\\"{long_item}\\"]"',
         "next": "fan"},
        {"id": "fan", "kind": "parallel", "worker": "w", "list_from": "ids", "next": None},
        {"id": "w", "kind": "work"}]}
    dead = run_until_killed(tmp_path, definition, stop_in=None,
                            env={driver.HANG_TASK_ENV: "three",
                                 driver.HANG_AFTER_ENV: "w=one,w=two"})
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    assert len(rec["resume_inputs"]["ids"]) == run_log.RESUME_INPUT_MAX_CHARS
    assert _resume(dead, definition, rec=rec).status == "completed"
    tasks = [s["task"] for s in _given(dead.workspace, "w")]
    assert len(tasks) == 6 and sorted(tasks[:3]) == sorted(tasks[3:])
    assert max(len(t) for t in tasks) > run_log.RESUME_INPUT_MAX_CHARS


def test_a_cut_parallel_step_reading_long_branch_ids_starts_the_same_branches(tmp_path):
    definition = {"name": "w", "start": "pick", "nodes": [
        {"id": "pick", "kind": "script",
         "command": f"{_pad(run_log.RESUME_INPUT_MAX_CHARS + 500)}; echo; "
                    """echo '["left","right"]'""",
         "next": "fan"},
        {"id": "fan", "kind": "parallel", "branches_from": "pick",
         "branches": ["left", "right", "spare"], "next": None},
        {"id": "left", "kind": "work"},
        {"id": "right", "kind": "work"},
        {"id": "spare", "kind": "work"}]}
    dead = run_until_killed(tmp_path, definition, stop_in="right",
                            env={driver.HANG_AFTER_ENV: "left"})
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    assert len(rec["resume_inputs"]["pick"]) == run_log.RESUME_INPUT_MAX_CHARS
    assert _resume(dead, definition, rec=rec).status == "completed"
    m = marks(dead.workspace)
    assert (m["left"], m["right"], m["pick"]) == (2, 2, 1) and "spare" not in m


# ── what a dead run's conversion, refusals and write-back undo leave in telemetry ──

def test_a_conversion_is_logged_and_counted_once(tmp_path, telemetry_dir):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="b")
    recorded_scripts = len(run_log.script_records(dead.workspace, "w", "r1"))
    assert run_log.mark_dead_run(dead.workspace, "w", "r1") is not None
    assert run_log.mark_dead_run(dead.workspace, "w", "r1") is None      # already converted
    assert payloads(telemetry_dir, "workflow.run_found_dead") == [
        {"workflow": "w", "run_id": "r1", "status": "crashed", "step": "b",
         "scripts_recorded": recorded_scripts}]
    assert_clean(recorded(telemetry_dir))


def test_a_conversion_counts_the_script_records_the_run_left(tmp_path, telemetry_dir):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="b")
    folder = run_log._record_path(dead.workspace, "w", "r1").parent
    already = len(run_log.script_records(dead.workspace, "w", "r1"))
    for pid in (2**22 + 11, 2**22 + 12):                   # long-gone processes
        run_log.record_script(folder, "r1", pid=pid, started="never", node_id="b")
    run_log.mark_dead_run(dead.workspace, "w", "r1")
    [found] = payloads(telemetry_dir, "workflow.run_found_dead")
    assert found["scripts_recorded"] == already + 2
    assert_clean(recorded(telemetry_dir))


def test_a_run_someone_stopped_is_counted_as_cancelled(tmp_path, telemetry_dir):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="b")
    assert run_log.request_stop(dead.workspace, "w", "r1", hard=False)
    run_log.mark_dead_run(dead.workspace, "w", "r1")
    [found] = payloads(telemetry_dir, "workflow.run_found_dead")
    assert found["status"] == "cancelled" and found["step"] == "b"


def test_a_conversion_on_the_sweep_thread_is_recorded_not_dropped(tmp_path, telemetry_dir):
    import threading
    import time

    from durin.telemetry.logger import current_telemetry

    dead = run_until_killed(tmp_path, CHAIN, stop_in="b")
    seen: list = []

    def sweep():
        seen.append(current_telemetry())          # nothing is bound on this thread
        run_log.reconcile_running(dead.workspace, now=time.time(),
                                  max_age_s=run_log.RECONCILE_AGE_S)
        seen.append(current_telemetry())

    thread = threading.Thread(target=sweep)
    thread.start()
    thread.join(30)
    assert seen == [None, None]                   # and none is left bound after it
    [found] = payloads(telemetry_dir, "workflow.run_found_dead")
    assert found["run_id"] == "r1"
    assert list(telemetry_dir.glob("*.jsonl"))    # on disk, in the instance's telemetry


def test_a_conversion_inside_a_bound_session_goes_to_that_sessions_log(tmp_path, telemetry_dir):
    from durin.telemetry.logger import bind_telemetry, reset_telemetry

    class Sink:
        def __init__(self):
            self.events = []

        def log(self, event_type, data):
            self.events.append((event_type, dict(data)))

    dead = run_until_killed(tmp_path, CHAIN, stop_in="b")
    sink = Sink()
    token = bind_telemetry(sink)
    try:
        run_log.mark_dead_run(dead.workspace, "w", "r1")
    finally:
        reset_telemetry(token)
    assert [t for t, _ in sink.events] == ["workflow.run_found_dead"]
    assert not telemetry_dir.exists() or not list(telemetry_dir.glob("*.jsonl"))


def _refusal_of(dead, definition=CHAIN, **over):
    rec = run_log.read_manifest(dead.workspace, definition["name"], "r1")
    return resume_refusal(dict(rec, **over), workspace=dead.workspace,
                          workflow=parse_workflow(definition))


def test_each_refusal_of_a_dead_run_names_its_reason(tmp_path):
    from durin.workflow.engine import (
        AUTOMATION_RUN_REFUSAL,
        INCOMPLETE_RECORD_REFUSAL,
        MCP_TOOLS_REFUSAL,
        mcp_tools_refusal,
    )

    dead = run_until_killed(tmp_path / "a", CHAIN, stop_in="c")
    run_log.mark_dead_run(dead.workspace, "w", "r1")
    assert _refusal_of(dead, launch={"kind": "automation"}).kind == "automation"
    assert _refusal_of(dead, launch={"kind": "automation"}) == AUTOMATION_RUN_REFUSAL
    assert _refusal_of(dead, incomplete=True).kind == "other"
    assert _refusal_of(dead, incomplete=True) == INCOMPLETE_RECORD_REFUSAL
    assert _refusal_of(dead, parent_run_id="p").kind == "other"
    rec = run_log.read_manifest(dead.workspace, "w", "r1")
    no_position = {k: v for k, v in rec.items() if k != "position"}
    assert resume_refusal(no_position, workspace=dead.workspace).kind == "other"
    assert resume_refusal(dict(rec, failed_node=None), workspace=dead.workspace).kind == "other"
    smaller = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "tools": "default", "next": None}]})
    gone = resume_refusal(rec, workspace=dead.workspace, workflow=smaller)
    assert gone.kind == "step_gone" and isinstance(gone, str) and "'c'" in gone
    with_mcp = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "mcps": ["tracker"], "next": None}]})
    mcp = mcp_tools_refusal(dict(rec, surface="agent"), with_mcp, workspace=dead.workspace)
    assert mcp.kind == "mcp_tools" and mcp == MCP_TOOLS_REFUSAL
    shutil.rmtree(dead.workspace / ".workflow" / "r1")
    assert resume_refusal(rec, workspace=dead.workspace,
                          workflow=parse_workflow(CHAIN)).kind == "folder_pruned"


def test_a_run_a_later_one_took_over_is_refused_as_superseded(tmp_path):
    keyed = {driver.WORK_KEY_ENV: "ticket-7"}
    dead = run_until_killed(tmp_path, CHAIN, stop_in="c", env=keyed)
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    run_to_end(tmp_path, CHAIN, env={**keyed, driver.RUN_ID_ENV: "r2"})
    assert resume_refusal(rec, workspace=dead.workspace,
                          workflow=parse_workflow(CHAIN)).kind == "superseded"


def test_a_failed_run_in_a_parallel_node_is_refused_for_another_reason(tmp_path):
    rec = {"status": "aborted", "failed_node": "p", "runs": []}
    assert resume_refusal(rec).kind == "other"


def test_what_a_dead_process_left_behind_names_its_reason(tmp_path, monkeypatch):
    from durin.utils import process_tree

    dead = run_until_killed(tmp_path, CHAIN, stop_in="b")
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    folder = run_log._record_path(dead.workspace, "w", "r1").parent
    # the record of the killed script, whose process the harness ended
    assert run_log.dead_run_refusal(dead.workspace, rec) is None

    # a record that changed since it was read: another caller got there first
    assert run_log.dead_run_refusal(dead.workspace, dict(rec, task="other")).kind == "lost_race"

    # a crashed record that still names a live process as the one running it
    marked = dict(rec, owner=process_tree.process_identity())
    run_log._write_json(run_log._record_path(dead.workspace, "w", "r1"), marked)
    refusal = run_log.dead_run_refusal(dead.workspace, marked)
    assert refusal.kind == "owner_alive"
    run_log._write_json(run_log._record_path(dead.workspace, "w", "r1"), rec)

    # a script that may still run
    path = run_log.record_script(folder, "r1", pid=2**22 + 5, started="x", node_id="b")
    for state in ("alive", "unknown"):
        monkeypatch.setattr(process_tree, "left_script_state",
                            lambda pid, started, state=state, **kw: (state, [7], [8]))
        refusal = run_log.dead_run_refusal(dead.workspace, rec)
        assert refusal.kind == "script_alive"
    # a script record nobody can read
    path.write_text("{not json", encoding="utf-8")
    assert run_log.dead_run_refusal(dead.workspace, rec).kind == "other"
    path.unlink()

    # a sub-workflow run still recorded as running
    run_log.start_run(dead.workspace, "kid", "r-kid", root_session_key="s", started_at=2.0,
                      parent_run_id="r1")
    assert run_log.dead_run_refusal(dead.workspace, rec).kind == "child_running"


def test_a_lost_claim_is_a_refusal_with_its_reason(tmp_path):
    from durin.workflow.resume_claim import ResumeClaim, ResumeLostError

    dead = run_until_killed(tmp_path, CHAIN, stop_in="b")
    rec = run_log.mark_dead_run(dead.workspace, "w", "r1")
    ResumeClaim.take(dead.workspace, "w", "r1", expected=rec)
    with pytest.raises(ResumeLostError) as lost:
        ResumeClaim.take(dead.workspace, "w", "r1", expected=rec)
    assert lost.value.kind == "lost_race" and "already resumed" in str(lost.value)


def test_a_write_back_put_back_before_a_resumed_walk_is_counted(tmp_path, telemetry_dir):
    dead = run_until_killed(tmp_path, LOGS, stop_in=None, env={HANG_IN_APPLY_ENV: "1"})
    _resume(dead, LOGS)
    assert payloads(telemetry_dir, "workflow.writeback_restored") == [
        {"workflow": "w", "run_id": "r1", "step": "fan"}]
    assert_clean(recorded(telemetry_dir))


def test_a_resume_with_no_write_back_to_put_back_counts_none(tmp_path, telemetry_dir):
    dead = run_until_killed(tmp_path, CHAIN, stop_in="b")
    _resume(dead)
    assert payloads(telemetry_dir, "workflow.writeback_restored") == []
