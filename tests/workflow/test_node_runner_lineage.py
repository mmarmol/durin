"""A headless workflow run (no calling session) must not orphan its node sessions:
they share a synthetic run-root session ``workflow:<run_id>:root`` as their parent,
so ``children_of`` of that root finds every node session of the run."""

from unittest.mock import AsyncMock, MagicMock

from durin.agent.runner import AgentRunResult, AgentRunner
from durin.providers.base import LLMProvider
from durin.session import lineage
from durin.session.manager import Session, SessionManager
from durin.workflow.engine import NodeRunRequest
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import WorkNode


def _runner(sessions):
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    ar = AgentRunner(provider)
    ar.run = AsyncMock(return_value=AgentRunResult(
        final_content="done",
        messages=[{"role": "assistant", "content": "done"}],
    ))
    return AgentNodeRunner(ar, sessions, default_model="test-model")


def test_headless_node_sessions_share_a_run_root(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    nr = _runner(sessions)
    run_root = "workflow:rh:root"

    for node_id in ("a", "b"):
        nr(NodeRunRequest(
            node=WorkNode(id=node_id, prompt="p.", next=None),
            task="t", upstream_output=None, shared_context=[],
            run_id="rh", iteration=1, root_session_key=None,
        ))

    fresh = SessionManager(workspace=tmp_path)
    a = fresh.get_or_create("workflow:rh:a:1")
    b = fresh.get_or_create("workflow:rh:b:1")
    # Both node sessions point at the synthetic run-root, NOT at their own keys.
    assert lineage.parent_of(a.metadata) == run_root
    assert lineage.parent_of(b.metadata) == run_root
    # The run-root session was created.
    assert fresh.exists(run_root)
    # And it now parents every node session of the run.
    kids = {k["key"] for k in fresh.children_of(run_root)}
    assert kids == {"workflow:rh:a:1", "workflow:rh:b:1"}


def test_rooted_run_is_unchanged(tmp_path):
    sessions = SessionManager(workspace=tmp_path)
    sessions.save(Session(key="websocket:abc"))   # the calling session (root)
    nr = _runner(sessions)
    nr(NodeRunRequest(
        node=WorkNode(id="a", prompt="p.", next=None),
        task="t", upstream_output=None, shared_context=[],
        run_id="rk", iteration=1, root_session_key="websocket:abc",
    ))
    fresh = SessionManager(workspace=tmp_path)
    a = fresh.get_or_create("workflow:rk:a:1")
    # With a real calling session, lineage is unchanged: parent is that session,
    # and no synthetic run-root is created.
    assert lineage.parent_of(a.metadata) == "websocket:abc"
    assert not fresh.exists("workflow:rk:root")
