"""Tests for the approval pause: a WorkNode.approval=True node pauses the walk
with status='needs_input', ask_kind='approval' instead of threading its output
onward, and the reply (approve/revise) resumes it via durin.workflow.approval."""

from durin.workflow import run_log
from durin.workflow.approval import build_approval_resume
from durin.workflow.engine import NodeRunRequest, NodeRunResponse, ResumeState, WorkflowEngine
from durin.workflow.spec import parse_workflow


def _engine(node_outputs):
    """Engine with a scripted node runner. node_outputs: dict node_id -> output string."""
    calls = []

    def node_runner(req: NodeRunRequest) -> NodeRunResponse:
        calls.append(req.node.id)
        return NodeRunResponse(output=node_outputs[req.node.id])

    eng = WorkflowEngine(
        node_runner=node_runner,
        run_id_factory=lambda: "r1",
    )
    return eng, calls


def _engine_capturing(node_outputs, *, workspace):
    """Like _engine, but with a real workspace (so the manifest — and its
    resume_inputs — actually gets written) and returning the full NodeRunRequest
    objects, not just node ids, so a test can inspect exactly what composed
    upstream text a node received."""
    reqs = []

    def node_runner(req: NodeRunRequest) -> NodeRunResponse:
        reqs.append(req)
        return NodeRunResponse(output=node_outputs[req.node.id])

    eng = WorkflowEngine(
        node_runner=node_runner,
        run_id_factory=lambda: "r1",
        workspace=str(workspace),
    )
    return eng, reqs


WF = {
    "name": "d", "start": "draft",
    "nodes": [
        {"id": "draft", "kind": "work", "approval": True, "next": "send", "max_visits": 3},
        {"id": "send", "kind": "work", "next": None},
    ],
}


def test_approval_node_pauses_with_proposal():
    wf = parse_workflow(WF)
    eng, calls = _engine({"draft": "the drafted email", "send": "sent"})
    res = eng.run(wf, "chase invoice")
    assert res.status == "needs_input"
    assert res.ask_kind == "approval"
    assert res.needs_input_node == "draft"
    assert res.final_output == "the drafted email"     # the proposal
    assert calls == ["draft"]                           # send never ran


def test_approve_resumes_past_the_node():
    wf = parse_workflow(WF)
    manifest = {"run_id": "r1", "needs_input_node": "draft", "ask_kind": "approval",
                "final_output": "the drafted email", "resume_upstream": "chase invoice",
                "work_key": None, "runs": [{"node_id": "draft", "iteration": 1}]}
    state = build_approval_resume(wf, manifest, "approve", "")
    assert state.start_at == "send"
    assert state.upstream == "the drafted email"
    eng, calls = _engine({"draft": "never", "send": "sent"})
    res = eng.run(wf, "ignored", resume=state)
    assert res.status == "completed"
    assert calls == ["send"]


def test_revise_reruns_the_node_with_feedback():
    wf = parse_workflow(WF)
    manifest = {"run_id": "r1", "needs_input_node": "draft", "ask_kind": "approval",
                "final_output": "draft v1", "resume_upstream": "chase invoice",
                "work_key": None, "runs": [{"node_id": "draft", "iteration": 1}]}
    state = build_approval_resume(wf, manifest, "revise", "drop the amount from the subject")
    assert state.start_at == "draft"
    assert "drop the amount" in state.upstream and "chase invoice" in state.upstream
    eng, calls = _engine({"draft": "draft v2", "send": "sent"})
    res = eng.run(wf, "ignored", resume=state)
    assert res.status == "needs_input" and res.ask_kind == "approval"
    assert res.final_output == "draft v2"               # asks again with the new proposal
    assert calls == ["draft"]


def test_approve_on_terminal_approval_returns_none_state():
    wf = parse_workflow({"name": "d", "start": "a",
                         "nodes": [{"id": "a", "kind": "work", "approval": True, "next": None}]})
    manifest = {"run_id": "r1", "needs_input_node": "a", "ask_kind": "approval",
                "final_output": "proposal", "resume_upstream": "t", "work_key": None, "runs": []}
    assert build_approval_resume(wf, manifest, "approve", "") is None


def test_approve_resume_carries_forward_other_sources_resume_inputs(tmp_path):
    """a -> b(approval, next=c) -> c, with c.inputs_from=["a"]: after approve, c
    must still see a's real recorded output (not "(no output recorded)"), because
    the resumed walk's own in-memory trace never re-runs a — only the manifest's
    resume_inputs (written at the pause) still knows what a produced."""
    wf = parse_workflow({
        "name": "e", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "b"},
            {"id": "b", "kind": "work", "approval": True, "next": "c"},
            {"id": "c", "kind": "work", "next": None, "inputs_from": ["a"]},
        ],
    })
    eng, reqs = _engine_capturing(
        {"a": "a's real output", "b": "the drafted email", "c": "sent"}, workspace=tmp_path)
    res = eng.run(wf, "chase invoice")
    assert res.status == "needs_input" and res.ask_kind == "approval"

    manifest = run_log.read_manifest(tmp_path, "e", res.run_id)
    assert manifest["resume_inputs"] == {"a": "a's real output"}   # sanity: the pause recorded it

    state = build_approval_resume(wf, manifest, "approve", "")
    eng2, reqs2 = _engine_capturing({"a": "never", "b": "never", "c": "sent"}, workspace=tmp_path)
    res2 = eng2.run(wf, "ignored", resume=state)
    assert res2.status == "completed"

    c_req = next(r for r in reqs2 if r.node.id == "c")
    assert "a's real output" in c_req.upstream_output
    assert "(no output recorded)" not in c_req.upstream_output
    assert "the drafted email" in c_req.upstream_output   # the proposal, as upstream


def test_revise_resume_carries_forward_resume_inputs_for_the_approval_node_itself(tmp_path):
    """a -> b(approval, inputs_from=["a"], next=None): a revise re-runs b, and b's
    own composed input must still contain a's real recorded output."""
    wf = parse_workflow({
        "name": "f", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "b"},
            {"id": "b", "kind": "work", "approval": True, "next": None, "inputs_from": ["a"]},
        ],
    })
    eng, reqs = _engine_capturing(
        {"a": "a's real output", "b": "draft v1"}, workspace=tmp_path)
    res = eng.run(wf, "chase invoice")
    assert res.status == "needs_input" and res.ask_kind == "approval"

    manifest = run_log.read_manifest(tmp_path, "f", res.run_id)
    assert manifest["resume_inputs"] == {"a": "a's real output"}

    state = build_approval_resume(wf, manifest, "revise", "make it shorter")
    eng2, reqs2 = _engine_capturing({"a": "never", "b": "draft v2"}, workspace=tmp_path)
    res2 = eng2.run(wf, "ignored", resume=state)
    assert res2.status == "needs_input" and res2.ask_kind == "approval"
    assert res2.final_output == "draft v2"

    b_req = next(r for r in reqs2 if r.node.id == "b")
    assert "a's real output" in b_req.upstream_output
    assert "(no output recorded)" not in b_req.upstream_output
    assert "make it shorter" in b_req.upstream_output
