"""Tests for the default subworkflow runner (load + nested engine + depth cap)."""

import json

from durin.workflow.engine import NodeRunResponse, WorkflowEngine
from durin.workflow.loader import workflows_dir
from durin.workflow.spec import parse_workflow
from durin.workflow.subworkflow import SubworkflowRunner


def _write(workspace, name, data):
    d = workflows_dir(workspace)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")


def _node_runner(output):
    def nr(req):
        return NodeRunResponse(output=output, session_key=None, messages=[])
    return nr


def test_runs_named_workflow_and_returns_final_output(tmp_path):
    _write(tmp_path, "child", {"name": "child", "start": "a",
                               "nodes": [{"id": "a", "kind": "work", "next": None}]})
    runner = SubworkflowRunner(tmp_path, _node_runner("child-result"), judge_runner=None)
    out = runner("child", "do the child")
    assert out.status == "completed"
    assert out.final_output == "child-result"


def test_missing_subworkflow_returns_error_not_raise(tmp_path):
    runner = SubworkflowRunner(tmp_path, _node_runner("x"), judge_runner=None)
    out = runner("ghost", "t")
    assert out.status == "aborted"
    assert "ghost" in (out.final_output or "") or "Error" in (out.final_output or "")


def test_depth_cap_stops_deep_non_cyclic_nesting(tmp_path):
    # A calls B, B calls C, C calls D — no cycle, but deep enough to hit the depth cap.
    # The cap returns an error string at the limit.
    _write(tmp_path, "A", {"name": "A", "start": "s",
                           "nodes": [{"id": "s", "kind": "subworkflow", "workflow": "B", "next": None}]})
    _write(tmp_path, "B", {"name": "B", "start": "s",
                           "nodes": [{"id": "s", "kind": "subworkflow", "workflow": "C", "next": None}]})
    _write(tmp_path, "C", {"name": "C", "start": "s",
                           "nodes": [{"id": "s", "kind": "subworkflow", "workflow": "D", "next": None}]})
    _write(tmp_path, "D", {"name": "D", "start": "s",
                           "nodes": [{"id": "s", "kind": "work", "next": None}]})
    runner = SubworkflowRunner(tmp_path, _node_runner("x"), judge_runner=None, max_depth=2)
    out = runner("A", "t")
    assert out.status == "aborted"
    assert "depth" in (out.final_output or "").lower()


def test_subworkflow_cycle_is_detected(tmp_path):
    # workflow A has a subworkflow node calling A again; the call-stack guard
    # must stop on reentry with a clear cycle error, not a generic depth error.
    _write(tmp_path, "A", {"name": "A", "start": "call",
                           "nodes": [{"id": "call", "kind": "subworkflow", "workflow": "A", "next": None}]})
    runner = SubworkflowRunner(tmp_path, _node_runner("x"), judge_runner=None)
    out = runner("A", task="go")
    assert out.status == "aborted"
    assert "cycle detected" in (out.final_output or "")
    assert "A -> A" in (out.final_output or "")


def test_parent_run_id_forwarded_to_nested_manifest(tmp_path):
    from durin.workflow import run_log

    _write(tmp_path, "child", {"name": "child", "start": "a",
                               "nodes": [{"id": "a", "kind": "work", "next": None}]})
    runner = SubworkflowRunner(tmp_path, _node_runner("child-result"), judge_runner=None)
    runner("child", "do the child", parent_run_id="parent1")

    wf_dir = tmp_path / "workflows-runs" / "child"
    manifests = list(wf_dir.glob("*.json"))
    assert len(manifests) == 1
    rec = run_log.read_manifest(tmp_path, "child", manifests[0].stem)
    assert rec["parent_run_id"] == "parent1"


def test_parent_run_id_defaults_to_none(tmp_path):
    from durin.workflow import run_log

    _write(tmp_path, "child", {"name": "child", "start": "a",
                               "nodes": [{"id": "a", "kind": "work", "next": None}]})
    runner = SubworkflowRunner(tmp_path, _node_runner("child-result"), judge_runner=None)
    runner("child", "do the child")

    wf_dir = tmp_path / "workflows-runs" / "child"
    manifests = list(wf_dir.glob("*.json"))
    rec = run_log.read_manifest(tmp_path, "child", manifests[0].stem)
    assert rec["parent_run_id"] is None


def test_subworkflow_runs_script_nodes(tmp_path):
    # "tr a-z A-Z" reads stdin, proving the parent's task text ("abc") flows
    # through as the script node's stdin at the start of the child workflow —
    # with no agent node runner involved at all (the sentinel below would raise).
    from durin.workflow.script_runner import ScriptNodeRunner

    _write(tmp_path, "child", {
        "name": "child", "start": "s",
        "nodes": [{"id": "s", "kind": "script", "command": "tr a-z A-Z", "next": None}],
    })
    runner = SubworkflowRunner(
        tmp_path,
        node_runner=lambda req: (_ for _ in ()).throw(AssertionError("no agent node here")),
        judge_runner=None,
        script_runner=ScriptNodeRunner(tmp_path),
    )
    assert (runner("child", "abc").final_output or "").strip() == "ABC"


def test_nested_nodes_work_in_the_parent_folder(tmp_path):
    _write(tmp_path, "child", {
        "name": "child", "start": "c",
        "nodes": [{"id": "c", "kind": "work", "tools": "default", "next": None}],
    })
    seen = {}
    def node_runner(req):
        seen["c"] = req.output_dir
        return NodeRunResponse(output="child-out")
    parent_work = tmp_path / "parent-work"
    parent_work.mkdir()
    out = SubworkflowRunner(tmp_path, node_runner)("child", "task", None, work_dir=str(parent_work))
    assert out.final_output == "child-out"
    assert seen["c"] == str(parent_work)


def test_nested_run_emits_progress_tagged_with_its_parent_node(tmp_path):
    """Without the tag a surface cannot tell a nested frame from a top-level one
    and would render the sub-workflow's nodes as siblings of the caller's."""
    _write(tmp_path, "child", {"name": "child", "start": "a",
                               "nodes": [{"id": "a", "kind": "work", "next": None}]})
    emitted = []
    runner = SubworkflowRunner(tmp_path, _node_runner("child-result"), judge_runner=None)

    runner("child", "do the child", progress_emit=emitted.append, parent_node_id="call-child")

    frames = [f for p in emitted for f in p["nodes"]]
    assert frames, "the nested engine emitted nothing"
    assert all(f["parent_node"] == "call-child" for f in frames)


def test_nested_frames_are_re_keyed_onto_the_parent_run(tmp_path):
    """Surfaces key a work item by the frame's run id. A nested frame carrying the
    nested engine's OWN id opens a second item that the terminal frame (emitted for
    the caller's run id only) never closes — a phantom "running" entry."""
    _write(tmp_path, "child", {"name": "child", "start": "a",
                               "nodes": [{"id": "a", "kind": "work", "next": None}]})
    emitted = []
    runner = SubworkflowRunner(tmp_path, _node_runner("child-result"), judge_runner=None)

    runner("child", "do the child", progress_emit=emitted.append,
           parent_run_id="parent1", parent_node_id="call-child")

    assert emitted, "the nested engine emitted nothing"
    assert {p["run_id"] for p in emitted} == {"parent1"}
    # Only the emitted payload is re-keyed: the nested run still owns its own
    # manifest under its own id.
    stems = [p.stem for p in (tmp_path / "workflows-runs" / "child").glob("*.json")]
    assert stems and "parent1" not in stems


def test_nested_frames_keep_their_run_id_without_a_parent_run(tmp_path):
    """A sub-workflow invoked with no parent run (a standalone call) has no run to
    re-key onto, so its frames keep the nested engine's own id."""
    _write(tmp_path, "child", {"name": "child", "start": "a",
                               "nodes": [{"id": "a", "kind": "work", "next": None}]})
    emitted = []
    runner = SubworkflowRunner(tmp_path, _node_runner("child-result"), judge_runner=None)

    runner("child", "do the child", progress_emit=emitted.append)

    stems = {p.stem for p in (tmp_path / "workflows-runs" / "child").glob("*.json")}
    assert {p["run_id"] for p in emitted} == stems


def test_nested_progress_is_silent_when_the_parent_is_not_listening(tmp_path):
    _write(tmp_path, "child", {"name": "child", "start": "a",
                               "nodes": [{"id": "a", "kind": "work", "next": None}]})
    runner = SubworkflowRunner(tmp_path, _node_runner("x"), judge_runner=None)
    # No progress_emit: must run normally, not construct a tagging wrapper.
    assert runner("child", "t").final_output == "x"


def test_cancelling_the_parent_stops_the_nested_run(tmp_path):
    """A /stop on the caller must reach inside the sub-workflow, not burn tokens
    to completion with nobody waiting for the result."""
    _write(tmp_path, "child", {"name": "child", "start": "a",
                               "nodes": [{"id": "a", "kind": "work", "next": "b"},
                                         {"id": "b", "kind": "work", "next": None}]})
    ran = []

    def _counting_runner(req):
        ran.append(req.node.id)
        return NodeRunResponse(output="x", session_key=None, messages=[])

    runner = SubworkflowRunner(tmp_path, _counting_runner, judge_runner=None)
    runner("child", "t", cancel_check=lambda: len(ran) >= 1)

    assert ran == ["a"], f"nested run continued past the cancel: {ran}"


def test_engine_forwards_live_progress_and_cancel_to_a_real_subworkflow(tmp_path):
    """Regression guard for durin/workflow/engine.py's SubworkflowNode branch.

    Every other test that reaches that call site builds its outer engine with
    progress_emit=None, cancel_check=None, so the forwarding itself is never
    exercised with live values, and a fake subworkflow_runner elsewhere swallows
    the new kwargs via **_kwargs without inspecting them. This wires a REAL
    SubworkflowRunner into a REAL parent WorkflowEngine with LIVE progress_emit
    and cancel_check (mirroring durin/agent/tools/run_workflow.py's production
    wiring, which shares one node_runner between the parent engine and the
    SubworkflowRunner) and asserts through both layers: the child's own frames
    arrive at the parent's emitter tagged with the parent node's id, and
    cancelling the parent halts the child mid-graph. A dropped or swapped
    progress_emit/cancel_check at that call site must fail this test. Every frame
    the parent's emitter sees — its own and the child's — must be keyed by the
    parent's run id, or the child's frames become a second, never-ending work item.

    The parent workflow has a node after the subworkflow call so this test's
    outcome does not depend on how the parent reports its OWN terminal status
    when cancelled mid-subworkflow — that is covered separately in test_engine.py.
    """
    _write(tmp_path, "child", {"name": "child", "start": "a",
                               "nodes": [{"id": "a", "kind": "work", "next": "b"},
                                         {"id": "b", "kind": "work", "next": None}]})
    parent_wf = parse_workflow({
        "name": "parent", "start": "call-child",
        "nodes": [
            {"id": "call-child", "kind": "subworkflow", "workflow": "child", "next": "after"},
            {"id": "after", "kind": "work", "next": None},
        ],
    })
    ran: list = []

    def _counting_runner(req):
        ran.append(req.node.id)
        return NodeRunResponse(output="x", session_key=None, messages=[])

    emitted: list = []
    sub_runner = SubworkflowRunner(tmp_path, _counting_runner, judge_runner=None)
    engine = WorkflowEngine(
        node_runner=_counting_runner,
        subworkflow_runner=sub_runner,
        progress_emit=emitted.append,
        cancel_check=lambda: len(ran) >= 1,
    )

    result = engine.run(parent_wf, "t")

    # Only the child's own nodes ("a", "b") are checked for the tag — the parent's
    # own frames (for "call-child"/"after") are correctly untagged, since they are
    # not nested under anything.
    child_frames = [f for p in emitted for f in p["nodes"] if f["id"] in ("a", "b")]
    assert child_frames, "the nested engine emitted nothing"
    assert all(f.get("parent_node") == "call-child" for f in child_frames)
    assert {p["run_id"] for p in emitted} == {result.run_id}
    assert ran == ["a"], f"nested run continued past the cancel: {ran}"
