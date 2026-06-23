"""The default node runner runs an agent turn and persists the node session."""

from unittest.mock import AsyncMock, MagicMock, patch

from durin.agent.runner import AgentRunResult
from durin.providers.base import LLMProvider
from durin.session import lineage
from durin.session.manager import Session, SessionManager
from durin.workflow.engine import NodeRunRequest
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import WorkNode


def _runner(sessions, fake_result):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    from durin.agent.runner import AgentRunner
    ar = AgentRunner(provider)
    ar.run = AsyncMock(return_value=fake_result)
    return AgentNodeRunner(ar, sessions, default_model="test-model")


def test_runs_node_and_persists_session_with_lineage(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    sessions.save(Session(key="websocket:abc"))  # the invoking session (root)
    fake = AgentRunResult(
        final_content="node done",
        messages=[{"role": "user", "content": "t"}, {"role": "assistant", "content": "node done"}],
    )
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="build", model=None, context="own", prompt="Build it.", next=None),
        task="make X", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key="websocket:abc",
    )
    resp = nr(req)
    assert resp.output == "node done"
    assert resp.session_key == "workflow:r1:build:1"

    reloaded = SessionManager(workspace=tmp_path).get_or_create("workflow:r1:build:1")
    assert lineage.parent_of(reloaded.metadata) == "websocket:abc"
    assert reloaded.metadata[lineage.ORIGIN_TYPE] == "workflow_node"
    assert reloaded.metadata[lineage.ORIGIN_ID] == "r1:build:1"
    assert any(m.get("content") == "node done" for m in reloaded.messages)


def test_upstream_output_is_in_the_user_turn(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="ok", messages=[{"role": "assistant", "content": "ok"}])
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="b", prompt="Review.", next=None),
        task="review the work", upstream_output="PRIOR-OUTPUT", shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    user_turns = [m for m in spec.initial_messages if m["role"] == "user"]
    assert any("PRIOR-OUTPUT" in m["content"] for m in user_turns)


def test_node_model_overrides_default(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    fake = AgentRunResult(final_content="ok", messages=[])
    nr = _runner(sessions, fake)
    req = NodeRunRequest(
        node=WorkNode(id="a", model="big-model", next=None),
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    nr(req)
    spec = nr.runner.run.call_args.args[0]
    assert spec.model == "big-model"
