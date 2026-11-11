"""A step's allowed passes are counted over the whole run, by passes that
completed. An attempt that failed keeps its number, so its conversation is
never overwritten, and does not use up a pass."""
import json
from pathlib import Path

from durin.workflow.engine import (
    NodeExecutionError,
    NodeRunResponse,
    WorkflowEngine,
    build_resume_state,
)
from durin.workflow.spec import parse_workflow


class _Runner:
    def __init__(self, fail=None, outputs=None):
        self.fail, self.outputs, self.calls = dict(fail or {}), outputs or {}, []

    def __call__(self, req):
        self.calls.append((req.node.id, req.iteration, req.pass_number, req.budget))
        if self.fail.get(req.node.id, 0) > 0:
            self.fail[req.node.id] -= 1
            raise NodeExecutionError(req.node.id, req.iteration, None, RuntimeError("transient"))
        return NodeRunResponse(output=self.outputs.get(req.node.id, f"{req.node.id}-out"))

    def attempts(self, node):
        return [c[1] for c in self.calls if c[0] == node]


def _engine(tmp_path, runner):
    return WorkflowEngine(node_runner=runner, run_id_factory=lambda: "r1", workspace=str(tmp_path))


def _manifest(tmp_path):
    return json.loads((Path(tmp_path) / "workflows-runs" / "w" / "r1.json").read_text())


def _resume(tmp_path, wf, runner):
    return _engine(tmp_path, runner).run(wf, "t", resume=build_resume_state(_manifest(tmp_path), ""))


def test_a_step_that_failed_on_its_last_allowed_pass_is_retried(tmp_path):
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "max_visits": 1, "next": None}]})
    runner = _Runner(fail={"a": 1})
    assert _engine(tmp_path, runner).run(wf, "t").status == "aborted"
    assert _resume(tmp_path, wf, runner).status == "completed"
    assert runner.calls == [("a", 1, 1, 1), ("a", 2, 1, 1)]   # second attempt, still its first pass


def _loop():
    return parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "b"},
        {"id": "b", "kind": "work", "next": "c"},
        {"id": "c", "kind": "work", "on_pass": None, "on_fail": "a"}]})


def test_a_loop_after_two_resumes_never_reuses_a_pass_number(tmp_path):
    wf = _loop()
    runner = _Runner(fail={"b": 1, "c": 1}, outputs={"c": "needs work\nFAIL"})
    assert _engine(tmp_path, runner).run(wf, "t").status == "aborted"     # b fails
    assert _resume(tmp_path, wf, runner).status == "aborted"              # c fails
    last = _resume(tmp_path, wf, runner)                                  # the loop runs out
    assert last.status == "exhausted" and last.exhausted_node == "a"
    dispatched = [(n, it) for n, it, _p, _b in runner.calls]
    assert len(dispatched) == len(set(dispatched))                        # no key reused
    assert runner.attempts("a") == [1, 2, 3]                              # three completed passes
    assert runner.attempts("b") == [1, 2, 3, 4]                           # one failed, three completed
    assert runner.attempts("c") == [1, 2, 3, 4]


def test_the_loop_bound_holds_over_the_whole_run_not_the_last_walk(tmp_path):
    wf = _loop()
    runner = _Runner(fail={"b": 1, "c": 1}, outputs={"c": "needs work\nFAIL"})
    _engine(tmp_path, runner).run(wf, "t")
    _resume(tmp_path, wf, runner)
    _resume(tmp_path, wf, runner)
    passes = [p for n, _it, p, _b in runner.calls if n == "a"]
    assert passes == [1, 2, 3] and all(b == 3 for n, _i, _p, b in runner.calls if n == "a")


def test_the_final_record_holds_every_walks_rows_once(tmp_path):
    wf = _loop()
    runner = _Runner(fail={"b": 1, "c": 1}, outputs={"c": "needs work\nFAIL"})
    _engine(tmp_path, runner).run(wf, "t")
    _resume(tmp_path, wf, runner)
    _resume(tmp_path, wf, runner)
    rec = _manifest(tmp_path)
    rows = [(r["node_id"], r["iteration"]) for r in rec["history"] + rec["runs"]]
    assert len(rows) == len(set(rows))
    assert ("b", 1) in rows and ("c", 1) in rows                          # the failed attempts stay


def test_a_record_from_before_this_change_resumes_as_it_did(tmp_path):
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "b"}, {"id": "b", "kind": "work", "next": None}]})
    runner = _Runner(fail={"b": 1})
    _engine(tmp_path, runner).run(wf, "t")
    path = Path(tmp_path) / "workflows-runs" / "w" / "r1.json"
    rec = json.loads(path.read_text())
    for key in ("history", "passes"):
        rec.pop(key, None)
    path.write_text(json.dumps(rec))
    assert _resume(tmp_path, wf, runner).status == "completed"
    assert runner.attempts("a") == [1] and runner.attempts("b") == [1, 2]


def test_the_pass_note_follows_completed_passes_not_attempts():
    from types import SimpleNamespace

    from durin.workflow.node_runner import AgentNodeRunner
    retry = SimpleNamespace(budget=3, iteration=2, pass_number=1)
    assert AgentNodeRunner._pass_note(retry) == ""                        # a retry is not a second pass
    second = SimpleNamespace(budget=3, iteration=3, pass_number=2)
    assert "Pass 2 of 3" in AgentNodeRunner._pass_note(second)
    last = SimpleNamespace(budget=3, iteration=5, pass_number=3)
    assert "FINAL PASS (3 of 3)" in AgentNodeRunner._pass_note(last)


def _rows(tmp_path):
    rec = _manifest(tmp_path)
    return rec.get("history", []) + rec["runs"]


def test_a_retried_row_records_its_attempt_and_its_pass(tmp_path):
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "max_visits": 1, "next": None}]})
    runner = _Runner(fail={"a": 1})
    _engine(tmp_path, runner).run(wf, "t")
    _resume(tmp_path, wf, runner)
    retried = [r for r in _rows(tmp_path) if r["node_id"] == "a" and r["iteration"] == 2]
    assert len(retried) == 1
    assert retried[0]["pass_number"] == 1 and retried[0]["budget"] == 1


def test_a_loops_last_attempt_is_recorded_as_its_final_pass(tmp_path):
    wf = _loop()
    runner = _Runner(fail={"b": 1, "c": 1}, outputs={"c": "needs work\nFAIL"})
    _engine(tmp_path, runner).run(wf, "t")
    _resume(tmp_path, wf, runner)
    _resume(tmp_path, wf, runner)
    last = [r for r in _rows(tmp_path) if r["node_id"] == "b" and r["iteration"] == 4]
    assert len(last) == 1
    assert last[0]["pass_number"] == 3 and last[0]["budget"] == 3


def test_progress_frames_of_a_retried_step_carry_its_pass(tmp_path):
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "max_visits": 1, "next": None}]})
    runner = _Runner(fail={"a": 1})
    _engine(tmp_path, runner).run(wf, "t")
    frames: list[dict] = []
    eng = WorkflowEngine(node_runner=runner, run_id_factory=lambda: "r1",
                         workspace=str(tmp_path), progress_emit=frames.append)
    assert eng.run(wf, "t", resume=build_resume_state(_manifest(tmp_path), "")).status == "completed"
    seen = [n for f in frames for n in f["nodes"] if n["id"] == "a"]
    assert {n["status"] for n in seen} == {"running", "done"}
    assert all(n["iteration"] == 2 and n["pass_number"] == 1 and n["budget"] == 1 for n in seen)


def test_a_resumed_run_reports_the_files_written_before_it_stopped(tmp_path):
    wf = parse_workflow({"name": "w", "start": "a",
                         "output": {"artifacts": [{"path": "a.txt"}, {"path": "b.txt"}]},
                         "nodes": [{"id": "a", "kind": "work", "tools": "default", "next": "b"},
                                   {"id": "b", "kind": "work", "tools": "default", "next": None}]})

    class Writing(_Runner):
        def __call__(self, req):
            resp = super().__call__(req)                 # raises on a failing attempt
            Path(req.output_dir, f"{req.node.id}.txt").write_text("x")
            return resp

    runner = Writing(fail={"b": 1})
    assert _engine(tmp_path, runner).run(wf, "t").status == "aborted"
    done = _resume(tmp_path, wf, runner)
    assert done.status == "completed"
    assert done.output_files == ["a.txt", "b.txt"]
    assert done.missing_artifacts == []


def test_a_run_resumed_before_the_counts_existed_keeps_its_bound_over_two_more_resumes(tmp_path):
    # A two-step loop (budget 3) resumed once by an older version: its record has
    # no ``passes`` and its ``runs`` hold only that last walk, where ``a`` reached
    # attempt 2 and ``c`` failed its attempt 2. Attempts 1 ran in a walk the
    # record no longer holds, and completed.
    from durin.workflow import run_log
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "c"},
        {"id": "c", "kind": "work", "on_pass": None, "on_fail": "a"}]})
    run_log.start_run(tmp_path, "w", "r1", root_session_key=None, started_at=1.0)
    path = Path(tmp_path) / "workflows-runs" / "w" / "r1.json"
    rec = json.loads(path.read_text())
    for key in ("passes", "history"):
        rec.pop(key)
    rec.update(status="aborted", resumed=True, failed_node="c", resume_upstream="a-out",
               runs=[{"node_id": "a", "iteration": 2, "status": "ok"},
                     {"node_id": "c", "iteration": 2, "status": "node_failed"}])
    path.write_text(json.dumps(rec))

    runner = _Runner(fail={"c": 1}, outputs={"c": "needs work\nFAIL"})
    assert _resume(tmp_path, wf, runner).status == "aborted"             # c fails again
    assert "passes" in _manifest(tmp_path)                                # counted normally from here
    last = _resume(tmp_path, wf, runner)
    assert last.status == "exhausted" and last.exhausted_node == "a"
    # a completed attempts 1 and 2 before the upgrade: one pass is left.
    assert [(it, p) for n, it, p, _b in runner.calls if n == "a"] == [(3, 3)]
    assert [(it, p) for n, it, p, _b in runner.calls if n == "c"] == [(3, 2), (4, 2), (5, 3)]
