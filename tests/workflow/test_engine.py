"""Tests for the sequential flow-graph engine (graph logic, mocked node runner)."""

from durin.workflow.condition import CommandOutcome
from durin.workflow.engine import NodeRunRequest, NodeRunResponse, WorkflowEngine
from durin.workflow.spec import parse_workflow


def _engine(node_outputs, command_results):
    """Engine with a scripted node runner + scripted command results.

    node_outputs: dict node_id -> output string.
    command_results: list of bool (pass/fail), consumed in decision order.
    """
    calls = []

    def node_runner(req: NodeRunRequest) -> NodeRunResponse:
        calls.append(req)
        return NodeRunResponse(
            output=node_outputs[req.node.id],
            session_key=f"workflow:{req.run_id}:{req.node.id}:{req.iteration}",
            messages=[{"role": "assistant", "content": node_outputs[req.node.id]}],
        )

    results = iter(command_results)

    def command_runner(command, *, cwd=None, timeout=30):
        return CommandOutcome(passed=next(results), exit_code=0, output="")

    eng = WorkflowEngine(
        node_runner=node_runner,
        run_id_factory=lambda: "r1",
        command_runner=command_runner,
    )
    return eng, calls


def test_linear_two_nodes_complete():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "b"},
            {"id": "b", "kind": "work", "next": None},
        ],
    })
    eng, calls = _engine({"a": "out-a", "b": "out-b"}, [])
    res = eng.run(wf, "do it")
    assert res.status == "completed"
    assert res.final_output == "out-b"
    assert [r.node_id for r in res.runs] == ["a", "b"]
    assert res.runs[0].session_key == "workflow:r1:a:1"


def test_output_passes_downstream():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "b"},
            {"id": "b", "kind": "work", "next": None},
        ],
    })
    eng, calls = _engine({"a": "out-a", "b": "out-b"}, [])
    eng.run(wf, "do it")
    # b received a's output as upstream_output
    b_call = [c for c in calls if c.node.id == "b"][0]
    assert b_call.upstream_output == "out-a"


def test_decision_pass_continues():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "decision", "command": "x", "on_pass": "b", "on_fail": "a"},
            {"id": "b", "kind": "work", "next": None},
        ],
    })
    eng, calls = _engine({"a": "out-a", "b": "out-b"}, [True])
    res = eng.run(wf, "t")
    assert res.status == "completed"
    assert [r.node_id for r in res.runs] == ["a", "gate", "b"]
    gate_run = [r for r in res.runs if r.node_id == "gate"][0]
    assert gate_run.passed is True
    b_call = [c for c in calls if c.node.id == "b"][0]
    assert b_call.upstream_output == "out-a"


def test_decision_fail_loops_back_then_passes():
    wf = parse_workflow({
        "name": "d", "start": "a", "max_visits": 3,
        "nodes": [
            {"id": "a", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "decision", "command": "x", "on_pass": None, "on_fail": "a"},
        ],
    })
    eng, _ = _engine({"a": "out-a"}, [False, True])  # fail once, then pass
    res = eng.run(wf, "t")
    assert res.status == "completed"
    # a runs twice (iteration 1, then 2 after loop-back), gate twice
    assert [r.node_id for r in res.runs] == ["a", "gate", "a", "gate"]
    a_runs = [r for r in res.runs if r.node_id == "a"]
    assert [r.iteration for r in a_runs] == [1, 2]


def test_max_visits_aborts_infinite_loop():
    wf = parse_workflow({
        "name": "d", "start": "a", "max_visits": 2,
        "nodes": [
            {"id": "a", "kind": "work", "next": "gate"},
            {"id": "gate", "kind": "decision", "command": "x", "on_pass": None, "on_fail": "a"},
        ],
    })
    eng, _ = _engine({"a": "out-a"}, [False, False, False, False])  # never passes
    res = eng.run(wf, "t")
    assert res.status == "max_visits"


def test_shared_vs_own_context():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "context": "shared", "next": "b"},
            {"id": "b", "kind": "work", "context": "own", "next": None},
        ],
    })
    eng, calls = _engine({"a": "out-a", "b": "out-b"}, [])
    eng.run(wf, "t")
    a_call = [c for c in calls if c.node.id == "a"][0]
    b_call = [c for c in calls if c.node.id == "b"][0]
    # a (shared) starts with empty shared context; b (own) also sees no shared
    # context but a's message was appended to the shared buffer after a ran, so
    # b — being 'own' — must NOT receive it.
    assert a_call.shared_context == []
    assert b_call.shared_context == []   # own node: isolated from the shared buffer


def test_shared_buffer_accumulates_across_shared_nodes():
    wf = parse_workflow({
        "name": "d", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "context": "shared", "next": "b"},
            {"id": "b", "kind": "work", "context": "shared", "next": None},
        ],
    })
    eng, calls = _engine({"a": "out-a", "b": "out-b"}, [])
    eng.run(wf, "t")
    b_call = [c for c in calls if c.node.id == "b"][0]
    # b is the second shared node: it must receive a's appended message
    assert b_call.shared_context == [{"role": "assistant", "content": "out-a"}]


def test_judgment_decision_passes(monkeypatch):
    from durin.workflow.judge import JudgeVerdict
    wf = parse_workflow({"name": "d", "start": "a", "nodes": [
        {"id": "a", "kind": "work", "next": "g"},
        {"id": "g", "kind": "decision", "criteria": "ok?", "on_pass": None, "on_fail": "a"},
    ]})
    judged = []

    def judge_runner(criteria, output, model):
        judged.append((criteria, output, model))
        return JudgeVerdict(passed=True, feedback="PASS looks good")

    def node_runner(req):
        return NodeRunResponse(output="the work", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1", judge_runner=judge_runner)
    res = eng.run(wf, "t")
    assert res.status == "completed"
    assert judged and judged[0][0] == "ok?" and judged[0][1] == "the work"
    g_run = [r for r in res.runs if r.node_id == "g"][0]
    assert g_run.passed is True


def test_judgment_fail_loops_back_with_feedback():
    from durin.workflow.judge import JudgeVerdict
    wf = parse_workflow({"name": "d", "start": "a", "max_visits": 3, "nodes": [
        {"id": "a", "kind": "work", "next": "g"},
        {"id": "g", "kind": "decision", "criteria": "ok?", "on_pass": None, "on_fail": "a"},
    ]})
    verdicts = iter([JudgeVerdict(passed=False, feedback="FAIL: add validation"),
                     JudgeVerdict(passed=True, feedback="PASS")])
    seen_inputs = []

    def judge_runner(criteria, output, model):
        return next(verdicts)

    def node_runner(req):
        seen_inputs.append(req.upstream_output)
        return NodeRunResponse(output="attempt", session_key=None, messages=[])

    eng = WorkflowEngine(node_runner=node_runner, run_id_factory=lambda: "r1", judge_runner=judge_runner)
    res = eng.run(wf, "t")
    assert res.status == "completed"
    # the second run of 'a' (the loop-back) saw the reviewer feedback in its input
    assert any(inp and "add validation" in inp for inp in seen_inputs)
