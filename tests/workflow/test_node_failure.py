"""When a node's agent turn raises, its partial conversation is not lost: the node
runner persists whatever messages it built (status ``node_failed``) and raises a typed
``NodeExecutionError`` carrying the node id, iteration and persisted session key. The
engine records a ``node_failed`` NodeRun and finalizes an aborted result that names the
failing node."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.runner import AgentRunner
from durin.providers.base import LLMProvider
from durin.session.manager import SessionManager
from durin.workflow.engine import (
    NodeExecutionError,
    NodeRunRequest,
    NodeRunResponse,
    WorkflowEngine,
)
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import WorkNode, parse_workflow


def _two_node_wf():
    return parse_workflow({"name": "w", "start": "a", "max_visits": 3, "nodes": [
        {"id": "a", "kind": "work", "next": "b"},
        {"id": "b", "kind": "work", "next": None}]})


def test_failing_node_runner_aborts_and_names_the_node(tmp_path):
    # A node runner whose first node raises mid-turn must end the run as an aborted
    # result that names the failed node + iteration and records a node_failed NodeRun.
    def runner(req):
        if req.node.id == "a":
            raise NodeExecutionError("a", 1, "workflow:r1:a:1", RuntimeError("boom"))
        return NodeRunResponse(output="x")

    res = WorkflowEngine(runner, workspace=str(tmp_path),
                         run_id_factory=lambda: "r1").run(_two_node_wf(), "go")
    assert res.status == "aborted"
    assert res.failed_node == "a"
    assert res.failed_iteration == 1
    assert "a" in (res.final_output or "")
    failed = [r for r in res.runs if r.status == "node_failed"]
    assert len(failed) == 1
    assert failed[0].node_id == "a"
    assert failed[0].session_key == "workflow:r1:a:1"
    assert failed[0].error


def test_failed_noderun_is_in_manifest(tmp_path):
    # The failed node's NodeRun must be appended to result.runs so the finalized
    # manifest captures it.
    from durin.workflow import run_log

    def runner(req):
        raise NodeExecutionError("a", 1, "workflow:r1:a:1", RuntimeError("boom"))

    WorkflowEngine(runner, workspace=str(tmp_path),
                   run_id_factory=lambda: "r1").run(_two_node_wf(), "go")
    rec = run_log.read_manifest(tmp_path, "w", "r1")
    assert rec["status"] == "aborted"
    statuses = {r["node_id"]: r["status"] for r in rec["runs"]}
    assert statuses["a"] == "node_failed"


def _agent_runner_that_raises(sessions):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    ar = AgentRunner(provider)
    ar.run = AsyncMock(side_effect=RuntimeError("provider exploded"))
    return AgentNodeRunner(ar, sessions, default_model="test-model")


def test_node_runner_persists_partial_session_and_raises_typed(tmp_path):
    # When the real node runner's agent turn raises, it must persist whatever messages
    # it had (so the conversation is navigable) and raise a typed NodeExecutionError
    # carrying the persisted session key.
    sessions = SessionManager(workspace=tmp_path)
    nr = _agent_runner_that_raises(sessions)

    with pytest.raises(NodeExecutionError) as ei:
        nr(NodeRunRequest(
            node=WorkNode(id="a", prompt="do the thing.", next=None),
            task="t", upstream_output=None, shared_context=[],
            run_id="rh", iteration=1, root_session_key=None,
        ))
    err = ei.value
    assert err.node_id == "a"
    assert err.iteration == 1
    assert err.session_key == "workflow:rh:a:1"

    # The partial session exists and carries the prompt messages we built.
    fresh = SessionManager(workspace=tmp_path)
    sess = fresh.get_or_create("workflow:rh:a:1")
    assert any(m.get("role") == "user" for m in sess.messages)
