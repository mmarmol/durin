"""Tests for the approval pause: a WorkNode.approval=True node pauses the walk
with status='needs_input', ask_kind='approval' instead of threading its output
onward, and the reply (approve/revise) resumes it via durin.workflow.approval."""

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
