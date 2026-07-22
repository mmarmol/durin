"""Resume an aborted run at its failed node.

Live motivation (mxHero box, 2026-07-22): three pipeline runs died at the very
first node on transient API errors, and each cost a full 15-minute re-run —
with the manifest and the shared working folder already holding everything the
run had produced. Resume was needs_input-only. Now an aborted run that names a
``failed_node`` can resume there: the manifest stores the EXACT upstream text
the node received (``resume_upstream``, verbatim — a retried script parses its
stdin, so no retry framing may pollute it), and re-entry consumes the next
visit honestly.
"""

import json

from durin.workflow import run_log
from durin.workflow.engine import (
    NodeExecutionError,
    NodeRunResponse,
    WorkflowEngine,
    build_resume_state,
)
from durin.workflow.spec import parse_workflow


def _wf():
    return parse_workflow({"name": "d", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "b"},
        {"id": "b", "kind": "work", "next": "c"},
        {"id": "c", "kind": "work", "next": None},
    ]})


def _manifest(tmp_path, run_id):
    return json.loads(
        (tmp_path / "workflows-runs" / "d" / f"{run_id}.json").read_text(encoding="utf-8"))


def test_abort_records_the_failed_nodes_exact_upstream(tmp_path):
    def node_runner(req):
        if req.node.id == "b":
            raise NodeExecutionError("b", req.iteration, None, RuntimeError("transient"))
        return NodeRunResponse(output=f"{req.node.id}-out")

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1",
                         workspace=str(tmp_path))
    res = eng.run(_wf(), "t")
    assert res.status == "aborted" and res.failed_node == "b"
    m = _manifest(tmp_path, "r1")
    assert m["status"] == "aborted"
    assert m["failed_node"] == "b"
    assert m["resume_upstream"] == "a-out"        # verbatim, no framing


def test_build_resume_state_for_an_aborted_run_uses_the_stored_upstream():
    manifest = {
        "run_id": "r1",
        "status": "aborted",
        "failed_node": "b",
        "resume_upstream": "a-out",
        "final_output": "workflow aborted: node 'b' ...",
        "runs": [{"node_id": "a", "iteration": 1}, {"node_id": "b", "iteration": 1}],
    }
    resume = build_resume_state(manifest, "")
    assert resume.start_at == "b"
    assert resume.upstream == "a-out"             # EXACT — a script's stdin must not change
    assert resume.visits == {"a": 1, "b": 1}


def test_resumed_run_reruns_only_the_failed_node_with_original_input(tmp_path):
    attempts = {"b": 0}
    seen = {}

    def node_runner(req):
        if req.node.id == "b":
            attempts["b"] += 1
            if attempts["b"] == 1:
                raise NodeExecutionError("b", req.iteration, None, RuntimeError("transient"))
            seen["b_upstream"] = req.upstream_output
            seen["b_iteration"] = req.iteration
        if req.node.id == "a":
            seen.setdefault("a_runs", 0)
            seen["a_runs"] += 1
        return NodeRunResponse(output=f"{req.node.id}-out")

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1",
                         workspace=str(tmp_path))
    first = eng.run(_wf(), "t")
    assert first.status == "aborted"

    resume = build_resume_state(_manifest(tmp_path, "r1"), "")
    second = eng.run(_wf(), "t", resume=resume)
    assert second.status == "completed"
    assert seen["a_runs"] == 1                    # a never re-ran
    assert seen["b_upstream"] == "a-out"          # original input, verbatim
    assert seen["b_iteration"] == 2               # the retry consumed the next visit
    assert second.final_output == "c-out"


def test_resume_upstream_is_capped(tmp_path):
    big = "x" * 50_000

    def node_runner(req):
        if req.node.id == "b":
            raise NodeExecutionError("b", req.iteration, None, RuntimeError("boom"))
        return NodeRunResponse(output=big)

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1",
                         workspace=str(tmp_path))
    eng.run(_wf(), "t")
    stored = _manifest(tmp_path, "r1")["resume_upstream"]
    assert len(stored) <= run_log.RESUME_UPSTREAM_MAX_CHARS + 100
    assert stored.startswith("xxx")
