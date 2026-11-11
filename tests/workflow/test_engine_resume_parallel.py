"""Resuming a run that stopped at, or before, a parallel node.

A resume retries the run's failed node with the input it had. Three things must
hold for that to be true around parallel nodes:

- the failed node is one the walk dispatches. A branch only exists inside its
  parallel node: when a choose/union branch fails, the node to retry is the
  parallel node, whole, because the branches' private copies were thrown away;
- the record keeps the failed node's exact input and a row for the visit it
  consumed, also when the failed node is a sub-workflow;
- a resumed walk starts with an empty trace, so a later node that names an
  earlier one as the source of its list or of its branch ids reads that source
  from the record — and never parses a stored copy that was cut.
"""

import json
from pathlib import Path

from durin.workflow import run_log
from durin.workflow.engine import (
    NodeExecutionError,
    NodeRunResponse,
    WorkflowEngine,
    WorkInterruptedError,
    build_resume_state,
    resume_refusal,
)
from durin.workflow.result import WorkflowResult
from durin.workflow.spec import parse_workflow


def _manifest(tmp_path, name="w", run_id="r1"):
    return json.loads((Path(tmp_path) / "workflows-runs" / name / f"{run_id}.json").read_text())


def _engine(tmp_path, runner, **kw):
    return WorkflowEngine(node_runner=runner, run_id_factory=lambda: "r1",
                          workspace=str(tmp_path), **kw)


class _Runner:
    """Records every dispatch; ``fail_once`` names nodes whose first pass raises."""

    def __init__(self, fail_once=(), outputs=None, cause=None, fail_times=None):
        self.calls: list[tuple[str, int, str | None]] = []
        self._fail = {**{n: 1 for n in fail_once}, **(fail_times or {})}
        self._outputs = outputs or {}
        self._cause = cause

    def __call__(self, req):
        self.calls.append((req.node.id, req.iteration, req.upstream_output))
        if self._fail.get(req.node.id, 0) > 0:
            self._fail[req.node.id] -= 1
            raise NodeExecutionError(req.node.id, req.iteration, None,
                                     self._cause or RuntimeError("transient"))
        if req.workspace_override:
            Path(req.workspace_override, f"{req.node.id}.txt").write_text(f"by {req.node.id}")
        return NodeRunResponse(output=self._outputs.get(req.node.id, f"{req.node.id}-out"))

    def ran(self, node_id):
        return [c for c in self.calls if c[0] == node_id]


def _writing_wf(reconcile, start="pre", **extra):
    nodes = [
        {"id": "fan", "kind": "parallel", "branches": ["a", "b"],
         "reconcile": reconcile, "next": "after", **extra},
        {"id": "a", "kind": "work", "tools": "default"},
        {"id": "b", "kind": "work", "tools": "default"},
        {"id": "after", "kind": "work", "next": None},
    ]
    if start == "pre":
        nodes.insert(0, {"id": "pre", "kind": "work", "next": "fan"})
    return parse_workflow({"name": "w", "start": start, "nodes": nodes})


# ── a failed choose/union branch: the parallel node is what failed ──

def test_a_failed_union_branch_names_the_parallel_node_and_keeps_its_input(tmp_path):
    res = _engine(tmp_path, _Runner(fail_once={"b"})).run(_writing_wf("union"), "t")
    assert res.status == "aborted"
    assert res.failed_node == "fan" and res.failed_iteration == 1
    assert "'b'" in res.final_output and "transient" in res.final_output   # still names the branch
    m = _manifest(tmp_path)
    assert m["failed_node"] == "fan"
    assert m["resume_upstream"] == "pre-out"
    assert resume_refusal(m) is None                       # this record is resumable
    fan = next(r for r in m["runs"] if r["node_id"] == "fan")
    assert fan["status"] == "node_failed" and fan["iteration"] == 1
    assert "'b'" in fan["error"]


def test_resuming_it_reruns_the_whole_parallel_node_and_goes_on(tmp_path):
    runner = _Runner(fail_once={"b"})
    eng = _engine(tmp_path, runner)
    wf = _writing_wf("union")
    assert eng.run(wf, "t").status == "aborted"
    runner.calls.clear()

    second = eng.run(wf, "t", resume=build_resume_state(_manifest(tmp_path), ""))
    assert second.status == "completed"
    assert second.final_output == "after-out"             # the walk went on past the node
    assert runner.ran("pre") == []                         # what finished before is not re-run
    assert runner.ran("a") == [("a", 2, "pre-out")]        # every branch again, next visit,
    assert runner.ran("b") == [("b", 2, "pre-out")]        # with the node's original input
    assert len(runner.ran("after")) == 1
    work = Path(tmp_path) / ".workflow" / "r1" / "work"
    assert (work / "a.txt").read_text() == "by a" and (work / "b.txt").read_text() == "by b"


def test_resuming_a_failed_choose_at_the_start_node(tmp_path):
    runner = _Runner(fail_once={"a"})
    eng = _engine(tmp_path, runner, pick_runner=lambda criteria, options, model: 1)
    wf = _writing_wf("choose", start="fan", criteria="best")
    first = eng.run(wf, "t")
    assert first.status == "aborted" and first.failed_node == "fan"
    runner.calls.clear()

    second = eng.run(wf, "t", resume=build_resume_state(_manifest(tmp_path), ""))
    assert second.status == "completed" and second.final_output == "after-out"
    assert [c[:2] for c in runner.ran("a")] == [("a", 2)]
    assert [c[:2] for c in runner.ran("b")] == [("b", 2)]


def test_a_cancelled_union_branch_still_ends_the_run_cancelled(tmp_path):
    runner = _Runner(fail_once={"b"}, cause=WorkInterruptedError("stopped"))
    res = _engine(tmp_path, runner).run(_writing_wf("union"), "t")
    assert res.status == "cancelled"
    assert res.failed_node is None


# ── records written before the parallel node was the failed node ──

def _aborted_record(failed_node, runs):
    return {"run_id": "r0", "workflow": "w", "status": "aborted", "failed_node": failed_node,
            "resume_upstream": None, "runs": runs}


def test_a_record_naming_a_branch_as_the_failed_node_is_refused():
    no_row = _aborted_record("b", [{"node_id": "pre", "iteration": 1, "status": "ok"}])
    branch_row = _aborted_record("b", [
        {"node_id": "pre", "iteration": 1, "status": "ok"},
        {"node_id": "b", "iteration": 1, "status": "node_failed", "branch_id": "b"}])
    for record in (no_row, branch_row):
        refusal = resume_refusal(record)
        assert refusal and "'b'" in refusal and "new run" in refusal


def test_records_that_can_be_resumed_are_not_refused():
    linear = _aborted_record("b", [{"node_id": "b", "iteration": 1, "status": "node_failed",
                                    "branch_id": None, "worker_index": None}])
    asked = {"run_id": "r0", "status": "needs_input", "needs_input_node": "gate", "runs": []}
    assert resume_refusal(linear) is None
    assert resume_refusal(asked) is None


# ── a resumed walk reads earlier sources from the record ──

def test_resumed_parallel_resolves_its_branches_from_a_non_adjacent_source(tmp_path):
    wf = parse_workflow({"name": "w", "start": "route", "nodes": [
        {"id": "route", "kind": "work", "next": "mid"},
        {"id": "mid", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "branches_from": "route", "branches": ["a", "b", "c"],
         "reconcile": "union", "next": None},
        {"id": "a", "kind": "work", "tools": "default"},
        {"id": "b", "kind": "work", "tools": "default"},
        {"id": "c", "kind": "work", "tools": "default"},
    ]})
    runner = _Runner(fail_once={"b"}, outputs={"route": '["a", "b"]', "mid": "nothing to see"})
    eng = _engine(tmp_path, runner)
    assert eng.run(wf, "t").failed_node == "fan"
    runner.calls.clear()

    second = eng.run(wf, "t", resume=build_resume_state(_manifest(tmp_path), ""))
    assert second.status == "completed"
    assert sorted(c[0] for c in runner.calls) == ["a", "b"]     # the routed ids, not none, not all


def test_stored_sources_survive_a_second_failure_after_a_resume(tmp_path):
    # A transient error repeats: abort, resume, abort again, resume. The second
    # abort is recorded by a walk whose trace never held the routing node, so the
    # record must carry forward what the first one stored.
    wf = parse_workflow({"name": "w", "start": "route", "nodes": [
        {"id": "route", "kind": "work", "next": "mid"},
        {"id": "mid", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "branches_from": "route", "branches": ["a", "b", "c"],
         "reconcile": "union", "next": None},
        {"id": "a", "kind": "work", "tools": "default"},
        {"id": "b", "kind": "work", "tools": "default"},
        {"id": "c", "kind": "work", "tools": "default"},
    ]})
    runner = _Runner(fail_times={"b": 2}, outputs={"route": '["a", "b"]', "mid": "nothing to see"})
    eng = _engine(tmp_path, runner)
    assert eng.run(wf, "t").failed_node == "fan"
    second = eng.run(wf, "t", resume=build_resume_state(_manifest(tmp_path), ""))
    assert second.status == "aborted" and second.failed_node == "fan"
    assert _manifest(tmp_path)["resume_inputs"]["route"] == '["a", "b"]'
    runner.calls.clear()

    third = eng.run(wf, "t", resume=build_resume_state(_manifest(tmp_path), ""))
    assert third.status == "completed"
    assert sorted(runner.calls) == [("a", 3, "nothing to see"), ("b", 3, "nothing to see")]


def _fan_out_after_a_failure(list_text):
    wf = parse_workflow({"name": "w", "start": "orch", "nodes": [
        {"id": "orch", "kind": "work", "next": "mid"},
        {"id": "mid", "kind": "work", "next": "fan"},
        {"id": "fan", "kind": "parallel", "worker": "dev", "list_from": "orch", "next": None},
        {"id": "dev", "kind": "work"},
    ]})
    return wf, _Runner(fail_once={"mid"}, outputs={"orch": list_text, "mid": "an unrelated line"})


def test_resumed_fan_out_reads_its_list_from_a_source_that_ran_before_the_resume_point(tmp_path):
    wf, runner = _fan_out_after_a_failure('["one", "two"]')
    eng = _engine(tmp_path, runner)
    assert eng.run(wf, "t").failed_node == "mid"
    assert resume_refusal(_manifest(tmp_path)) is None     # a linear failure stays resumable
    runner.calls.clear()

    second = eng.run(wf, "t", resume=build_resume_state(_manifest(tmp_path), ""))
    assert second.status == "completed"
    assert sorted(c[2] for c in runner.ran("dev")) == ["one", "two"]   # not mid's line


def test_resumed_fan_out_refuses_a_stored_list_that_was_cut(tmp_path):
    long_list = json.dumps([f"item {i} " + "x" * 200 for i in range(45)])
    assert len(long_list) > run_log.RESUME_INPUT_MAX_CHARS
    wf, runner = _fan_out_after_a_failure(long_list)
    eng = _engine(tmp_path, runner)
    assert eng.run(wf, "t").failed_node == "mid"
    runner.calls.clear()

    second = eng.run(wf, "t", resume=build_resume_state(_manifest(tmp_path), ""))
    assert second.status == "aborted"
    assert "'orch'" in second.final_output and "resumed" in second.final_output
    assert runner.ran("dev") == []                                    # no worker on a fragment


# ── a failed sub-workflow keeps the input it received ──

def test_resuming_a_failed_sub_workflow_gives_it_the_input_it_had(tmp_path):
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "sub"},
        {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": None},
    ]})
    given: list[str] = []

    def subworkflow_runner(name, task, root_session_key, **kw):
        given.append(task)
        if len(given) == 1:
            return WorkflowResult(status="aborted", final_output="Error: boom", runs=[], run_id="c1")
        return WorkflowResult(status="completed", final_output="child done", runs=[], run_id="c2")

    eng = _engine(tmp_path, _Runner(), subworkflow_runner=subworkflow_runner)
    first = eng.run(wf, "the task")
    assert first.failed_node == "sub"
    assert _manifest(tmp_path)["resume_upstream"] == "a-out"
    assert resume_refusal(_manifest(tmp_path)) is None     # so does a failed sub-workflow

    second = eng.run(wf, "the task", resume=build_resume_state(_manifest(tmp_path), ""))
    assert second.status == "completed" and second.final_output == "child done"
    assert given == ["a-out", "a-out"]


def test_a_failed_script_node_stays_resumable(tmp_path):
    from durin.workflow.script_runner import ScriptNodeRunner

    wf = parse_workflow({"name": "w", "start": "s", "nodes": [
        {"id": "s", "kind": "script", "command": "exit 3", "next": None}]})
    eng = WorkflowEngine(node_runner=_Runner(), script_runner=ScriptNodeRunner(tmp_path),
                         run_id_factory=lambda: "r1", workspace=str(tmp_path))
    assert eng.run(wf, "t").failed_node == "s"
    assert resume_refusal(_manifest(tmp_path)) is None
