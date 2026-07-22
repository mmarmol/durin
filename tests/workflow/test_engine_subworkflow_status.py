"""A sub-workflow child's terminal status must reach the parent run.

Live finding (mxHero box, 2026-07-22): a 4-stage pipeline "completed" with three
stages never executed — each child ended ``needs_input`` and the runner flattened
that to a plain string, so the parent recorded the node ok and threaded the guard
message as edge text. These tests pin the propagation contract: completed threads,
needs_input pauses the parent resumably, cancelled mirrors, aborted/exhausted
abort the parent naming the child. A plain-string return (legacy doubles/runners)
keeps meaning "completed with this output".
"""

from durin.workflow.engine import (
    NodeRunResponse,
    ResumeState,
    WorkflowEngine,
    build_resume_state,
)
from durin.workflow.result import WorkflowResult
from durin.workflow.spec import parse_workflow


def _pipeline_wf():
    return parse_workflow({"name": "pipe", "start": "sub", "nodes": [
        {"id": "sub", "kind": "subworkflow", "workflow": "child", "next": "after"},
        {"id": "after", "kind": "work", "next": None},
    ]})


def _node_runner(seen=None):
    def nr(req):
        if seen is not None:
            seen.append(req.upstream_output)
        return NodeRunResponse(output="after-out", session_key=None, messages=[])
    return nr


def _stub(result):
    def subworkflow_runner(name, task, root_session_key=None, work_dir=None,
                           parent_run_id=None, **_kwargs):
        return result
    return subworkflow_runner


def test_child_needs_input_pauses_the_parent_resumably():
    child = WorkflowResult(status="needs_input", final_output="Q1: which mailbox?",
                           runs=[], run_id="c1")
    eng = WorkflowEngine(node_runner=_node_runner(), run_id_factory=lambda: "r1",
                         subworkflow_runner=_stub(child))
    res = eng.run(_pipeline_wf(), "t")
    assert res.status == "needs_input"
    assert res.needs_input_node == "sub"
    assert "which mailbox" in (res.final_output or "")
    # the sub node's record exists so the manifest shows where the run stopped
    assert [r.node_id for r in res.runs] == ["sub"]
    assert res.runs[0].status == "ok"
    assert res.runs[0].duration_s is not None


def test_child_aborted_aborts_the_parent_naming_the_child():
    child = WorkflowResult(status="aborted", final_output="Error: boom",
                           runs=[], run_id="c1")
    eng = WorkflowEngine(node_runner=_node_runner(), run_id_factory=lambda: "r1",
                         subworkflow_runner=_stub(child))
    res = eng.run(_pipeline_wf(), "t")
    assert res.status == "aborted"
    assert "child" in (res.final_output or "")      # names the child workflow
    assert "boom" in (res.final_output or "")
    assert res.failed_node == "sub"
    assert res.runs[0].status == "node_failed"
    assert res.runs[0].error and "boom" in res.runs[0].error


def test_child_exhausted_aborts_the_parent_with_the_honest_reason():
    child = WorkflowResult(status="exhausted", final_output="gate never passed",
                           runs=[], run_id="c1")
    eng = WorkflowEngine(node_runner=_node_runner(), run_id_factory=lambda: "r1",
                         subworkflow_runner=_stub(child))
    res = eng.run(_pipeline_wf(), "t")
    # the PARENT's budget was not hit — it aborts, and the message says the child exhausted
    assert res.status == "aborted"
    assert "exhausted" in (res.final_output or "")


def test_child_cancelled_cancels_the_parent():
    child = WorkflowResult(status="cancelled", final_output="partial",
                           runs=[], run_id="c1")
    eng = WorkflowEngine(node_runner=_node_runner(), run_id_factory=lambda: "r1",
                         subworkflow_runner=_stub(child))
    res = eng.run(_pipeline_wf(), "t")
    assert res.status == "cancelled"


def test_child_completed_threads_output_and_records_duration():
    child = WorkflowResult(status="completed", final_output="child-out",
                           runs=[], run_id="c1")
    seen = []
    eng = WorkflowEngine(node_runner=_node_runner(seen), run_id_factory=lambda: "r1",
                         subworkflow_runner=_stub(child))
    res = eng.run(_pipeline_wf(), "t")
    assert res.status == "completed"
    assert seen and "child-out" in (seen[0] or "")
    sub_run = res.runs[0]
    assert sub_run.node_id == "sub" and sub_run.duration_s is not None


def test_legacy_plain_string_return_still_means_completed():
    eng = WorkflowEngine(node_runner=_node_runner(), run_id_factory=lambda: "r1",
                         subworkflow_runner=_stub("plain-child-output"))
    res = eng.run(_pipeline_wf(), "t")
    assert res.status == "completed"
    assert res.runs[0].output == "plain-child-output"


def test_needs_input_parent_resumes_by_rerunning_the_child_with_answers():
    calls = []

    def subworkflow_runner(name, task, root_session_key=None, work_dir=None,
                           parent_run_id=None, **_kwargs):
        calls.append(task)
        if len(calls) == 1:
            return WorkflowResult(status="needs_input", final_output="Q: which org?",
                                  runs=[], run_id="c1")
        return WorkflowResult(status="completed", final_output="done", runs=[], run_id="c1")

    eng = WorkflowEngine(node_runner=_node_runner(), run_id_factory=lambda: "r1",
                         subworkflow_runner=subworkflow_runner)
    wf = _pipeline_wf()
    first = eng.run(wf, "TICKET_ID=1")
    assert first.status == "needs_input" and first.needs_input_node == "sub"

    manifest = {
        "run_id": first.run_id,
        "needs_input_node": first.needs_input_node,
        "final_output": first.final_output,
        "runs": [{"node_id": r.node_id, "iteration": r.iteration} for r in first.runs],
    }
    resume = build_resume_state(manifest, "org is acme")
    assert isinstance(resume, ResumeState)
    second = eng.run(wf, "TICKET_ID=1", resume=resume)
    assert second.status == "completed"
    # the re-entered sub node received the framed answers as its task
    assert len(calls) == 2 and "org is acme" in calls[1]
