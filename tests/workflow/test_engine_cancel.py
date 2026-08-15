"""Cooperative cancellation: the engine checks ``cancel_check`` between nodes.

A graceful cancel takes effect at the top of the node walk — a node already
executing finishes first, but the next node never starts. A HARD cancel is
additionally handed to agent nodes so their in-flight turn can be aborted.
Either way the run ends ``cancelled`` with the partial per-node trace.
"""

from durin.workflow.engine import (
    NodeExecutionError,
    NodeRunRequest,
    NodeRunResponse,
    ScriptCancelled,
    WorkflowEngine,
    WorkInterrupted,
)
from durin.workflow.spec import parse_workflow


def _wf_two_nodes():
    return parse_workflow({
        "name": "cancelme", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "next": "b"},
            {"id": "b", "kind": "work", "next": None},
        ],
    })


def test_cancel_after_first_node_stops_before_second():
    state = {"cancel": False}
    ran = []

    def runner(req: NodeRunRequest) -> NodeRunResponse:
        ran.append(req.node.id)
        if req.node.id == "a":
            state["cancel"] = True  # ask to cancel once node a has run
        return NodeRunResponse(
            output=f"out-{req.node.id}",
            session_key=f"workflow:{req.run_id}:{req.node.id}:{req.iteration}",
            messages=[],
        )

    eng = WorkflowEngine(
        node_runner=runner,
        run_id_factory=lambda: "r1",
        cancel_check=lambda: state["cancel"],
    )
    result = eng.run(_wf_two_nodes(), "do it", root_session_key="websocket:chatA")

    assert result.status == "cancelled"
    assert ran == ["a"], "node b must never start once cancel is requested after a"
    assert [r.node_id for r in result.runs] == ["a"], "partial trace keeps node a"
    assert result.run_id == "r1"


def test_cancel_before_start_yields_empty_trace():
    def runner(req: NodeRunRequest) -> NodeRunResponse:  # pragma: no cover - never called
        raise AssertionError("no node should run when cancelled before start")

    eng = WorkflowEngine(
        node_runner=runner,
        run_id_factory=lambda: "r2",
        cancel_check=lambda: True,
    )
    result = eng.run(_wf_two_nodes(), "do it", root_session_key="websocket:chatA")

    assert result.status == "cancelled"
    assert result.runs == []


def test_no_cancel_check_completes_normally():
    def runner(req: NodeRunRequest) -> NodeRunResponse:
        return NodeRunResponse(
            output=f"out-{req.node.id}",
            session_key=f"workflow:{req.run_id}:{req.node.id}:{req.iteration}",
            messages=[],
        )

    eng = WorkflowEngine(node_runner=runner, run_id_factory=lambda: "r3")
    result = eng.run(_wf_two_nodes(), "do it", root_session_key="websocket:chatA")
    assert result.status == "completed"


def test_work_interrupted_cause_ends_run_cancelled():
    """A hard cancel aborts the in-flight work turn: the node runner raises
    NodeExecutionError with a WorkInterrupted cause, and the run must end
    'cancelled' (not 'aborted'), keeping the honest node_failed trace row."""

    def runner(req: NodeRunRequest) -> NodeRunResponse:
        raise NodeExecutionError(req.node.id, req.iteration, None, WorkInterrupted("forced"))

    eng = WorkflowEngine(node_runner=runner, run_id_factory=lambda: "r-hard")
    result = eng.run(_wf_two_nodes(), "do it", root_session_key="websocket:chatA")

    assert result.status == "cancelled"
    assert [r.status for r in result.runs] == ["node_failed"]


def _wf_work_then_script():
    return parse_workflow({
        "name": "modes", "start": "a",
        "nodes": [
            {"id": "a", "kind": "work", "prompt": "p", "next": "s"},
            {"id": "s", "kind": "script", "command": "cat", "next": None},
        ],
    })


def test_work_nodes_poll_the_hard_check_scripts_poll_the_plain_one():
    """The engine hands the HARD check to agent nodes (only a force-stop
    interrupts their turn) and the plain check to script nodes (their subprocess
    dies on either cancel mode)."""
    captured: dict = {}

    def runner(req: NodeRunRequest) -> NodeRunResponse:
        captured[req.node.id] = req.cancel_check
        return NodeRunResponse(output="x", session_key=None, messages=[])

    def plain() -> bool:
        return False

    def hard() -> bool:
        return False

    eng = WorkflowEngine(
        node_runner=runner, script_runner=runner, run_id_factory=lambda: "r-mode",
        cancel_check=plain, hard_cancel_check=hard,
    )
    result = eng.run(_wf_work_then_script(), "t", root_session_key="websocket:chatA")

    assert result.status == "completed"
    assert captured["a"] is hard
    assert captured["s"] is plain


def test_graceful_cancel_leaves_the_in_flight_work_turn_alone():
    """The whole point of the graceful default: a work node executing while a
    graceful stop lands must never see its own poll turn true, so its turn runs
    to completion. The run still ends cancelled — between nodes."""
    state = {"cancel": False}
    seen: list = []

    def runner(req: NodeRunRequest) -> NodeRunResponse:
        # A real node runner aborts its turn the moment this poll turns true;
        # here we record what the engine actually lets the node see.
        state["cancel"] = True   # the stop lands while node a is executing
        seen.append((req.node.id, req.cancel_check() if req.cancel_check else None))
        return NodeRunResponse(output="x", session_key=None, messages=[])

    eng = WorkflowEngine(
        node_runner=runner, run_id_factory=lambda: "r-graceful",
        cancel_check=lambda: state["cancel"],
        # A graceful stop never escalates on its own.
        hard_cancel_check=lambda: False,
    )
    result = eng.run(_wf_two_nodes(), "t", root_session_key="websocket:chatA")

    assert result.status == "cancelled"
    assert seen == [("a", False)], "a graceful stop must never make a work node's poll true"


def test_hard_cancel_reaches_parallel_branches():
    """Parallel branches are agent turns too — a force-stop must be able to
    interrupt them, so they get the hard check rather than nothing."""
    captured: dict = {}

    def runner(req: NodeRunRequest) -> NodeRunResponse:
        captured[req.node.id] = req.cancel_check
        return NodeRunResponse(output=f"out-{req.node.id}", session_key=None, messages=[])

    def hard() -> bool:
        return False

    eng = WorkflowEngine(
        node_runner=runner, run_id_factory=lambda: "r-par", hard_cancel_check=hard,
    )
    wf = parse_workflow({
        "name": "p", "start": "fan",
        "nodes": [
            {"id": "fan", "kind": "parallel", "branches": ["b1", "b2"], "next": None},
            {"id": "b1", "kind": "work", "prompt": "p", "next": None},
            {"id": "b2", "kind": "work", "prompt": "p", "next": None},
        ],
    })
    eng.run(wf, "t")

    assert captured["b1"] is hard
    assert captured["b2"] is hard


def test_a_parallel_node_whose_branches_were_all_interrupted_ends_cancelled():
    """Every branch failing normally aborts the run. When the failures are the
    force-stop the user asked for, "every branch failed" is the wrong account —
    the run was cancelled."""
    state = {"cancel": False}

    def runner(req: NodeRunRequest) -> NodeRunResponse:
        # The force-stop lands while the branches are in flight; each branch's
        # turn is aborted, which the branch layer records as a failure.
        state["cancel"] = True
        raise NodeExecutionError(req.node.id, req.iteration, None, WorkInterrupted("forced"))

    eng = WorkflowEngine(
        node_runner=runner, run_id_factory=lambda: "r-par-hard",
        cancel_check=lambda: state["cancel"],
        hard_cancel_check=lambda: state["cancel"],
    )
    wf = parse_workflow({
        "name": "p", "start": "fan",
        "nodes": [
            {"id": "fan", "kind": "parallel", "branches": ["b1", "b2"], "next": None},
            {"id": "b1", "kind": "work", "prompt": "p", "next": None},
            {"id": "b2", "kind": "work", "prompt": "p", "next": None},
        ],
    })
    result = eng.run(wf, "t", root_session_key="websocket:chatA")

    assert result.status == "cancelled"


def test_detached_nodes_get_the_same_checks_the_linear_walk_gives():
    """A detached node runs past the walk that launched it, so a stop that ended
    the run would otherwise leave its subprocess alive to outlive the run. It
    polls the plain check (either mode kills it) exactly like a linear script
    node; a detached agent node keeps the hard-only check."""
    captured: dict = {}

    def runner(req: NodeRunRequest) -> NodeRunResponse:
        captured[req.node.id] = req.cancel_check
        return NodeRunResponse(output=f"out-{req.node.id}", session_key=None, messages=[])

    def plain() -> bool:
        return False

    def hard() -> bool:
        return False

    eng = WorkflowEngine(
        node_runner=runner, script_runner=runner, run_id_factory=lambda: "r-det",
        cancel_check=plain, hard_cancel_check=hard,
    )
    wf = parse_workflow({
        "name": "d", "start": "s",
        "nodes": [
            {"id": "s", "kind": "script", "command": "cat", "detached": True, "next": "w"},
            {"id": "w", "kind": "work", "prompt": "p", "detached": True, "next": "end"},
            {"id": "end", "kind": "work", "prompt": "p", "next": None},
        ],
    })
    result = eng.run(wf, "t", root_session_key="websocket:chatA")

    assert result.status == "completed"
    assert captured["s"] is plain
    assert captured["w"] is hard


def _wf_parallel(*branches, kind="work"):
    nodes: list[dict] = [
        {"id": "fan", "kind": "parallel", "branches": list(branches), "next": None}]
    for b in branches:
        nodes.append({"id": b, "kind": "work", "prompt": "p", "next": None} if kind == "work"
                     else {"id": b, "kind": "script", "command": "cat", "next": None})
    return parse_workflow({"name": "p", "start": "fan", "nodes": nodes})


def test_a_graceful_stop_pending_does_not_relabel_a_real_parallel_failure():
    """A graceful stop leaves an in-flight agent branch alone, so branches that
    fail while one is pending failed on their own merits. Reporting that run as
    'cancelled' would hide a genuine failure behind the user's stop."""

    state = {"cancel": False}

    def runner(req: NodeRunRequest) -> NodeRunResponse:
        # The graceful stop lands while the branches are in flight; they then
        # fail for reasons of their own, which the stop must not relabel.
        state["cancel"] = True
        raise NodeExecutionError(req.node.id, req.iteration, None, RuntimeError("boom"))

    eng = WorkflowEngine(
        node_runner=runner, run_id_factory=lambda: "r-par-soft",
        cancel_check=lambda: state["cancel"],   # a graceful stop is pending
        hard_cancel_check=lambda: False,        # but nothing was interrupted
    )
    result = eng.run(_wf_parallel("b1", "b2"), "t", root_session_key="websocket:chatA")

    assert result.status == "aborted"
    assert "every branch failed" in result.final_output


def test_a_graceful_stop_that_killed_every_script_branch_ends_cancelled():
    """A graceful stop DOES kill a script branch's subprocess. When that is why
    every branch is gone, 'every branch failed' is the wrong account — nothing
    failed, the user stopped it."""

    state = {"cancel": False}

    def script_runner(req: NodeRunRequest) -> NodeRunResponse:
        state["cancel"] = True   # the stop lands while the subprocesses run
        raise NodeExecutionError(req.node.id, req.iteration, None,
                                 ScriptCancelled("cancelled by user"))

    eng = WorkflowEngine(
        node_runner=lambda req: NodeRunResponse(output="", session_key=None, messages=[]),
        script_runner=script_runner, run_id_factory=lambda: "r-par-script",
        cancel_check=lambda: state["cancel"], hard_cancel_check=lambda: False,
    )
    result = eng.run(_wf_parallel("s1", "s2", kind="script"), "t",
                     root_session_key="websocket:chatA")

    assert result.status == "cancelled"


def test_a_hard_stop_that_interrupted_every_fanout_worker_ends_cancelled():
    """The dynamic fan-out reports 'every worker failed' the same way a static
    parallel does — and needs the same correction when the failures are the
    force-stop the user asked for."""

    state = {"cancel": False}

    def runner(req: NodeRunRequest) -> NodeRunResponse:
        if req.node.id == "list":
            return NodeRunResponse(output="one, two", session_key=None, messages=[])
        state["cancel"] = True   # the force-stop lands while the workers run
        raise NodeExecutionError(req.node.id, req.iteration, None, WorkInterrupted("forced"))

    wf = parse_workflow({
        "name": "f", "start": "list",
        "nodes": [
            {"id": "list", "kind": "work", "prompt": "p", "next": "fan"},
            {"id": "fan", "kind": "parallel", "list_from": "list", "worker": "w", "next": None},
            {"id": "w", "kind": "work", "prompt": "p", "next": None},
        ],
    })
    eng = WorkflowEngine(
        node_runner=runner, run_id_factory=lambda: "r-fan-hard",
        cancel_check=lambda: state["cancel"], hard_cancel_check=lambda: state["cancel"],
    )
    result = eng.run(wf, "t", root_session_key="websocket:chatA")

    assert result.status == "cancelled"
