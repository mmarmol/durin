"""A run whose process died is resumed at the step it was in, with the input
that step had, and what had finished does not run again."""
import json
import shutil
from pathlib import Path

import pytest

from durin.workflow import run_log
from durin.workflow.diagnostics import compute_diagnostics
from durin.workflow.engine import build_resume_state, resumable_kind, resume_refusal
from durin.workflow.spec import parse_workflow
from tests.workflow import _crash_driver as driver
from tests.workflow.crash_harness import engine, marks, run_to_end, run_until_killed

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
