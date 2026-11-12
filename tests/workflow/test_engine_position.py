"""The record always names the step to enter next (``position``), and what that
step receives is stored whole beside the run, so a run whose process died can
be entered again exactly where it stood."""
import json
from pathlib import Path

from durin.workflow import resume_store, run_log
from durin.workflow.artifacts import keyed_work_dir
from durin.workflow.engine import (
    NodeExecutionError,
    NodeRunResponse,
    WorkflowEngine,
    build_resume_state,
)
from durin.workflow.loader import workflows_dir
from durin.workflow.spec import parse_workflow
from durin.workflow.subworkflow import SubworkflowRunner


class _Runner:
    """Answers each step with ``<id>-out`` (or a given output, or the next of a
    list of outputs), fails a step the given number of times, records what each
    step received, and calls ``probe`` (if any) at the start of every step,
    while the step is in flight."""

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


def _engine(tmp_path, runner, **kwargs):
    return WorkflowEngine(node_runner=runner, run_id_factory=lambda: "r1",
                          workspace=str(tmp_path), **kwargs)


def _manifest(tmp_path):
    return json.loads((Path(tmp_path) / "workflows-runs" / "w" / "r1.json").read_text())


def _store(tmp_path):
    return resume_store.store_dir_of(tmp_path, _manifest(tmp_path))


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
    return resume_store.load_input(resume_store.store_dir_of(tmp_path, rec), rec["position"]["seq"])


def test_while_a_step_runs_the_record_names_it_and_holds_its_input(tmp_path):
    # a -> b -> c; the runner, when called for b, reads the record and the store
    seen = {}

    def runner(req):
        if req.node.id == "b":
            rec = _manifest(tmp_path)
            seen["position"] = rec["position"]
            seen["input"] = resume_store.load_input(_store(tmp_path), rec["position"]["seq"])
        return NodeRunResponse(output="A" * 40_000 if req.node.id == "a" else f"{req.node.id}-out")

    _engine(tmp_path, runner).run(_chain(), "t")
    assert seen["position"]["node"] == "b"
    assert seen["input"] == ("b", "A" * 40_000)                 # whole, past the record's cap


def test_the_start_step_is_the_first_position(tmp_path):
    seen = {}

    def probe(req):
        if req.node.id == "a":
            seen["position"] = _manifest(tmp_path)["position"]
            seen["input"] = resume_store.load_input(_store(tmp_path), 1)

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
    assert resume_store.load_input(_store(tmp_path), rec["position"]["seq"]) == ("b", "a-out")


def test_a_failed_steps_retry_receives_its_whole_input(tmp_path):
    # b's input is longer than the record's capped copy; b fails, and its retry
    # receives the whole input, the one stored for the position, not the cap.
    whole = "x" * (run_log.RESUME_UPSTREAM_MAX_CHARS + 4_000)
    runner = _Runner(fail={"b": 1}, outputs={"a": whole})
    assert _engine(tmp_path, runner).run(_chain(), "t").status == "aborted"
    rec = _manifest(tmp_path)
    assert rec["failed_node"] == rec["position"]["node"] == "b"
    stored = resume_store.load_input(_store(tmp_path), rec["position"]["seq"])
    assert stored == ("b", whole)
    assert stored[1][:run_log.RESUME_UPSTREAM_MAX_CHARS] == rec["resume_upstream"]

    resume = build_resume_state(rec, "", workspace=tmp_path)
    assert resume.upstream == whole
    assert _engine(tmp_path, runner).run(_chain(), "t", resume=resume).status == "completed"
    assert runner.received[-2] == ("b", whole)


def test_a_failed_steps_retry_falls_back_to_the_capped_copy(tmp_path):
    # Without the stored file (no workspace given, the file gone, or a position
    # that names another step) the retry gets the record's capped copy.
    whole = "x" * (run_log.RESUME_UPSTREAM_MAX_CHARS + 4_000)
    assert _engine(tmp_path, _Runner(fail={"b": 1}, outputs={"a": whole})).run(
        _chain(), "t").status == "aborted"
    rec = _manifest(tmp_path)
    capped = rec["resume_upstream"]
    assert build_resume_state(rec, "").upstream == capped
    assert build_resume_state({**rec, "position": None}, "", workspace=tmp_path).upstream == capped
    assert build_resume_state({**rec, "position": {"node": "a", "seq": rec["position"]["seq"]}},
                              "", workspace=tmp_path).upstream == capped
    resume_store.discard(_store(tmp_path))
    assert build_resume_state(rec, "", workspace=tmp_path).upstream == capped


def test_a_named_source_is_stored_whole(tmp_path):
    wf = parse_workflow({"name": "w", "start": "plan", "nodes": [
        {"id": "plan", "kind": "work", "next": "mid"},
        {"id": "mid", "kind": "work", "next": "use"},
        {"id": "use", "kind": "work", "inputs_from": ["plan"], "next": None}]})
    plan = "p" * 20_000
    # `use` fails once so the aborted record keeps its own capped copy to compare with.
    runner = _Runner(fail={"use": 1}, outputs={"plan": plan})
    assert _engine(tmp_path, runner).run(wf, "t").status == "aborted"
    assert resume_store.load_sources(_store(tmp_path))["plan"] == plan
    capped = _manifest(tmp_path)["resume_inputs"]["plan"]
    assert len(capped) == run_log.RESUME_INPUT_MAX_CHARS < len(plan)


def test_a_source_that_outputs_nothing_on_a_later_pass_is_stored_empty(tmp_path):
    # src prints a list on its first pass and nothing on its second (a loop whose
    # list ran out): the walk reads "", so the store must too, never the old text.
    wf = parse_workflow({"name": "w", "start": "src", "nodes": [
        {"id": "src", "kind": "work", "next": "chk"},
        {"id": "chk", "kind": "work", "inputs_from": ["src"], "on_pass": None, "on_fail": "src"}]})
    seen = []

    def probe(req):
        if req.node.id == "chk":
            seen.append(resume_store.load_sources(_store(tmp_path)).get("src"))

    runner = _Runner(outputs={"src": ["one\ntwo", ""], "chk": ["more\nFAIL", "done\nPASS"]},
                     probe=probe)
    assert _engine(tmp_path, runner).run(wf, "t").status == "completed"
    assert seen == ["one\ntwo", ""]


def test_a_resumed_walk_continues_the_sequence(tmp_path, monkeypatch):
    seqs: list[int] = []
    real = resume_store.save_input

    def recording(store_dir, seq, node_id, upstream):
        seqs.append(seq)
        return real(store_dir, seq, node_id, upstream)

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

    def failing(store_dir, seq, node_id, upstream):
        if node_id == node:
            raise OSError("disk full")
        return real(store_dir, seq, node_id, upstream)

    monkeypatch.setattr(resume_store, "save_input", failing)


def test_an_input_that_cannot_be_stored_is_never_named(tmp_path, monkeypatch):
    # The input of c cannot be written: the run goes on, and while c runs the
    # record names no step at all — never c, whose input is not on disk, and
    # never b, which has finished.
    _failing_save_input(monkeypatch, "c")
    seen = {}

    def probe(req):
        if req.node.id == "c":
            seen["position"] = _manifest(tmp_path)["position"]

    assert _engine(tmp_path, _Runner(probe=probe)).run(_chain(), "t").status == "completed"
    assert seen["position"] is None


class _Died(BaseException):
    """What the walk sees of its process's death: nothing after it runs."""


def test_a_death_after_an_input_that_cannot_be_stored_is_refused(tmp_path, monkeypatch):
    # The input of c cannot be written, then the process dies while c runs. The
    # record names no step, so the conversion names no finished step as failed
    # and a resume is refused instead of running b, which finished, again.
    from durin.workflow.engine import resumable_kind, resume_refusal

    _failing_save_input(monkeypatch, "c")

    def probe(req):
        if req.node.id == "c":
            raise _Died()

    try:
        _engine(tmp_path, _Runner(probe=probe)).run(_chain(), "t")
    except _Died:
        pass
    left = _manifest(tmp_path)
    assert left["status"] == "running" and left["position"] is None
    assert left["active_node"]["node_id"] == "c"
    run_log._write_json(run_log._record_path(tmp_path, "w", "r1"),
                        {**left, "owner": {"pid": 2**22 + 4242, "started": "never"}})

    rec = run_log.mark_dead_run(tmp_path, "w", "r1")
    assert rec["status"] == "crashed" and "failed_node" not in rec
    assert resumable_kind(rec) is None
    assert resume_refusal(rec, workspace=tmp_path, workflow=_chain()) == (
        "the step it was in cannot be entered again. Start a new run instead.")


def test_a_start_input_that_cannot_be_stored_leaves_no_position(tmp_path, monkeypatch):
    _failing_save_input(monkeypatch, "a")
    seen = {}

    def probe(req):
        if req.node.id == "a":
            seen["position"] = _manifest(tmp_path)["position"]

    assert _engine(tmp_path, _Runner(probe=probe)).run(_chain(), "t").status == "completed"
    assert seen["position"] is None


# --- where the files live, and when they go away ---------------------------------


def _keyed_store(tmp_path):
    return keyed_work_dir(tmp_path, "w", "k1").parent / "resume" / "r1"


def test_a_keyed_run_that_aborts_keeps_its_inputs_beside_the_keyed_folder(tmp_path):
    result = _engine(tmp_path, _Runner(fail={"b": 1})).run(_chain(), "t", work_key="k1")
    assert result.status == "aborted"
    rec = _manifest(tmp_path)
    assert resume_store.store_dir_of(tmp_path, rec) == _keyed_store(tmp_path)
    assert resume_store.load_input(_keyed_store(tmp_path), rec["position"]["seq"]) == ("b", "a-out")
    assert not (tmp_path / ".workflow" / "r1").exists()        # no keep_runs slot taken


def test_a_completed_run_leaves_no_resume_files(tmp_path):
    assert _engine(tmp_path, _Runner()).run(_chain(), "t").status == "completed"
    assert not resume_store.store_dir(tmp_path, "r1").exists()
    assert (tmp_path / ".workflow" / "r1" / "work").is_dir()   # the working folder stays


def test_a_completed_keyed_run_leaves_no_resume_files(tmp_path):
    assert _engine(tmp_path, _Runner()).run(_chain(), "t", work_key="k1").status == "completed"
    assert not _keyed_store(tmp_path).exists()
    assert not (tmp_path / ".workflow" / "r1").exists()


def test_an_exhausted_run_leaves_no_resume_files(tmp_path):
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "max_visits": 1, "next": "gate"},
        {"id": "gate", "kind": "work", "on_pass": None, "on_fail": "a"}]})
    runner = _Runner(outputs={"gate": "no\nFAIL"})
    assert _engine(tmp_path, runner).run(wf, "t").status == "exhausted"
    assert not resume_store.store_dir(tmp_path, "r1").exists()
    assert _manifest(tmp_path)["position"] is None             # names no input that is gone


def test_a_cancelled_run_leaves_no_resume_files(tmp_path):
    state = {"cancel": False}

    def probe(req):
        if req.node.id == "a":
            state["cancel"] = True

    engine = _engine(tmp_path, _Runner(probe=probe), cancel_check=lambda: state["cancel"])
    assert engine.run(_chain(), "t").status == "cancelled"
    assert not resume_store.store_dir(tmp_path, "r1").exists()
    assert _manifest(tmp_path)["position"] is None


def test_a_paused_run_keeps_its_resume_files(tmp_path):
    wf = parse_workflow({"name": "w", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "ask"},
        {"id": "ask", "kind": "work", "cases": {"ASK": "__needs_input__", "GO": None}}]})
    runner = _Runner(outputs={"ask": "which one?\nASK"})
    assert _engine(tmp_path, runner).run(wf, "t").status == "needs_input"
    rec = _manifest(tmp_path)
    assert rec["position"]["node"] == "ask"
    assert resume_store.load_input(_store(tmp_path), rec["position"]["seq"]) == ("ask", "a-out")


def test_the_reader_finds_the_folder_the_engine_wrote_to(tmp_path, monkeypatch):
    written = []
    real = resume_store.save_input

    def recording(store_dir, seq, node_id, upstream):
        written.append(store_dir)
        return real(store_dir, seq, node_id, upstream)

    monkeypatch.setattr(resume_store, "save_input", recording)
    for key, workspace in ((None, tmp_path / "plain"), ("k1", tmp_path / "keyed")):
        written.clear()
        _engine(workspace, _Runner(fail={"b": 1})).run(_chain(), "t", work_key=key)
        assert set(written) == {resume_store.store_dir_of(workspace, _manifest(workspace))}


# --- a sub-workflow's own run --------------------------------------------------


def test_a_child_runs_record_never_has_a_position(tmp_path):
    workflows_dir(tmp_path).mkdir(parents=True)
    (workflows_dir(tmp_path) / "child.json").write_text(json.dumps({"name": "child", "start": "inner", "nodes": [
        {"id": "inner", "kind": "work", "next": "last"},
        {"id": "last", "kind": "work", "next": None}]}))
    parent = parse_workflow({"name": "w", "start": "call", "nodes": [
        {"id": "call", "kind": "subworkflow", "workflow": "child", "next": None}]})
    seen = []

    def child_record():
        [path] = (tmp_path / "workflows-runs" / "child").glob("*.json")
        return json.loads(path.read_text())

    def probe(req):
        seen.append((child_record().get("position"), _manifest(tmp_path)["position"]))

    runner = _Runner(probe=probe)
    engine = _engine(tmp_path, runner,
                     subworkflow_runner=SubworkflowRunner(tmp_path, runner, judge_runner=None))
    assert engine.run(parent, "t").status == "completed"
    assert [child for child, _ in seen] == [None, None]          # at every write, inside the child
    assert all(p["node"] == "call" for _, p in seen)            # while the parent's names its step
    assert child_record().get("position") is None               # and at the end
    assert {p.name for p in (tmp_path / ".workflow").iterdir() if p.is_dir()} == {"r1"}


# --- a run ended without the engine (an approval answered, a pause cancelled) ----


def _paused_record(tmp_path):
    run_log.start_run(tmp_path, "w", "r1", root_session_key=None, started_at=1.0,
                      work_dir=str(tmp_path / ".workflow" / "r1" / "work"),
                      position={"node": "approve", "seq": 3})
    store = resume_store.store_dir_of(tmp_path, _manifest(tmp_path))
    resume_store.save_input(store, 3, "approve", "the proposal")
    return store


def test_a_run_finished_in_place_has_no_position_and_no_resume_files(tmp_path):
    for status in ("completed", "cancelled"):
        store = _paused_record(tmp_path)
        rec = run_log.finalize_short_circuit(tmp_path, "w", "r1", status=status, final_output="x")
        assert rec["position"] is None and _manifest(tmp_path)["position"] is None
        assert not store.exists()
