"""The record always names the step to enter next (``position``), and what that
step receives is stored whole beside the run, so a run whose process died can
be entered again exactly where it stood."""
import json
from pathlib import Path

from durin.workflow import resume_store, run_log
from durin.workflow.engine import (
    NodeExecutionError,
    NodeRunResponse,
    WorkflowEngine,
    build_resume_state,
)
from durin.workflow.spec import parse_workflow


class _Runner:
    """Answers each step with ``<id>-out`` (or a given output), fails a step the
    given number of times, records what each step received, and calls ``probe``
    (if any) at the start of every step, while the step is in flight."""

    def __init__(self, fail=None, outputs=None, probe=None):
        self.fail = dict(fail or {})
        self.outputs = outputs or {}
        self.probe = probe
        self.received: list[tuple[str, str | None]] = []

    def __call__(self, req):
        self.received.append((req.node.id, req.upstream_output))
        if self.probe is not None:
            self.probe(req)
        if self.fail.get(req.node.id, 0) > 0:
            self.fail[req.node.id] -= 1
            raise NodeExecutionError(req.node.id, req.iteration, None, RuntimeError("transient"))
        out = self.outputs.get(req.node.id, f"{req.node.id}-out")
        if isinstance(out, list):
            out = out.pop(0)
        return NodeRunResponse(output=out)


def _engine(tmp_path, runner):
    return WorkflowEngine(node_runner=runner, run_id_factory=lambda: "r1", workspace=str(tmp_path))


def _manifest(tmp_path):
    return json.loads((Path(tmp_path) / "workflows-runs" / "w" / "r1.json").read_text())


def _chain():
    return parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "b"},
        {"id": "b", "kind": "work", "next": "c"},
        {"id": "c", "kind": "work", "next": None}]})


def _gated():
    return parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "gate"},
        {"id": "gate", "kind": "work", "on_pass": "c", "on_fail": "a"},
        {"id": "c", "kind": "work", "next": None}]})


def _stored_input(tmp_path):
    rec = _manifest(tmp_path)
    return resume_store.load_input(tmp_path, "r1", rec["position"]["seq"])


def test_while_a_step_runs_the_record_names_it_and_holds_its_input(tmp_path):
    # a -> b -> c; the runner, when called for b, reads the record and the store
    seen = {}

    def runner(req):
        if req.node.id == "b":
            rec = _manifest(tmp_path)
            seen["position"] = rec["position"]
            seen["input"] = resume_store.load_input(tmp_path, "r1", rec["position"]["seq"])
        return NodeRunResponse(output="A" * 40_000 if req.node.id == "a" else f"{req.node.id}-out")

    _engine(tmp_path, runner).run(_chain(), "t")
    assert seen["position"]["node"] == "b"
    assert seen["input"] == ("b", "A" * 40_000)                 # whole, past the record's cap


def test_the_start_step_is_the_first_position(tmp_path):
    seen = {}

    def probe(req):
        if req.node.id == "a":
            seen["position"] = _manifest(tmp_path)["position"]
            seen["input"] = resume_store.load_input(tmp_path, "r1", 1)

    _engine(tmp_path, _Runner(probe=probe)).run(_chain(), "t")
    assert seen["position"] == {"node": "a", "seq": 1}
    assert seen["input"] == ("a", None)


def test_a_gate_that_passes_hands_on_the_input_it_received(tmp_path):
    seen = {}

    def probe(req):
        if req.node.id == "c":
            seen["input"] = _stored_input(tmp_path)

    runner = _Runner(outputs={"gate": "ok\nPASS"}, probe=probe)
    assert _engine(tmp_path, runner).run(_gated(), "t").status == "completed"
    assert seen["input"] == ("c", "a-out")
    assert ("c", "a-out") in runner.received                   # what c was handed is what was stored


def test_a_gate_that_fails_hands_back_its_feedback(tmp_path):
    seen = []

    def probe(req):
        if req.node.id == "a":
            seen.append(_stored_input(tmp_path))

    runner = _Runner(outputs={"gate": ["needs more detail\nFAIL", "ok\nPASS"]}, probe=probe)
    assert _engine(tmp_path, runner).run(_gated(), "t").status == "completed"
    assert len(seen) == 2
    node, upstream = seen[1]                                    # the second pass of a
    assert node == "a"
    assert "Reviewer feedback" in upstream and "needs more detail\nFAIL" in upstream
    assert [u for n, u in runner.received if n == "a"][1] == upstream


def test_a_finished_run_has_no_position(tmp_path):
    assert _engine(tmp_path, _Runner()).run(_chain(), "t").status == "completed"
    assert _manifest(tmp_path)["position"] is None


def test_a_failed_step_is_still_the_position(tmp_path):
    assert _engine(tmp_path, _Runner(fail={"b": 1})).run(_chain(), "t").status == "aborted"
    rec = _manifest(tmp_path)
    assert rec["status"] == "aborted"
    assert rec["position"]["node"] == "b"
    assert resume_store.load_input(tmp_path, "r1", rec["position"]["seq"]) == ("b", "a-out")


def test_a_named_source_is_stored_whole(tmp_path):
    wf = parse_workflow({"name": "w", "start": "plan", "nodes": [
        {"id": "plan", "kind": "work", "next": "mid"},
        {"id": "mid", "kind": "work", "next": "use"},
        {"id": "use", "kind": "work", "inputs_from": ["plan"], "next": None}]})
    plan = "p" * 20_000
    # `use` fails once so the aborted record keeps its own capped copy to compare with.
    runner = _Runner(fail={"use": 1}, outputs={"plan": plan})
    assert _engine(tmp_path, runner).run(wf, "t").status == "aborted"
    assert resume_store.load_sources(tmp_path, "r1")["plan"] == plan
    capped = _manifest(tmp_path)["resume_inputs"]["plan"]
    assert len(capped) == run_log.RESUME_INPUT_MAX_CHARS < len(plan)


def test_a_resumed_walk_continues_the_sequence(tmp_path, monkeypatch):
    seqs: list[int] = []
    real = resume_store.save_input

    def recording(workspace, run_id, seq, node_id, upstream):
        seqs.append(seq)
        return real(workspace, run_id, seq, node_id, upstream)

    monkeypatch.setattr(resume_store, "save_input", recording)
    runner = _Runner(fail={"b": 1})
    assert _engine(tmp_path, runner).run(_chain(), "t").status == "aborted"
    first = len(seqs)
    resume = build_resume_state(_manifest(tmp_path), "")
    assert _engine(tmp_path, runner).run(_chain(), "t", resume=resume).status == "completed"
    assert 0 < first < len(seqs)                                # both walks saved inputs
    assert all(x < y for x, y in zip(seqs, seqs[1:]))           # strictly increasing


def _failing_save_input(monkeypatch, node):
    real = resume_store.save_input

    def failing(workspace, run_id, seq, node_id, upstream):
        if node_id == node:
            raise OSError("disk full")
        return real(workspace, run_id, seq, node_id, upstream)

    monkeypatch.setattr(resume_store, "save_input", failing)


def test_an_input_that_cannot_be_stored_is_never_named(tmp_path, monkeypatch):
    # The input of c cannot be written: the run goes on, and while c runs the
    # record still names b, whose input is on disk.
    _failing_save_input(monkeypatch, "c")
    seen = {}

    def probe(req):
        if req.node.id == "c":
            seen["position"] = _manifest(tmp_path)["position"]

    assert _engine(tmp_path, _Runner(probe=probe)).run(_chain(), "t").status == "completed"
    assert seen["position"]["node"] == "b"
    assert resume_store.load_input(tmp_path, "r1", seen["position"]["seq"]) == ("b", "a-out")


def test_a_start_input_that_cannot_be_stored_leaves_no_position(tmp_path, monkeypatch):
    _failing_save_input(monkeypatch, "a")
    seen = {}

    def probe(req):
        if req.node.id == "a":
            seen["position"] = _manifest(tmp_path)["position"]

    assert _engine(tmp_path, _Runner(probe=probe)).run(_chain(), "t").status == "completed"
    assert seen["position"] is None
