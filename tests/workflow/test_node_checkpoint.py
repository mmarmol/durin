"""Tests for mid-node checkpointing: a node's conversation is persisted every
round, not only when its agent turn returns.

Covers two layers: NodeCheckpointHook itself (does it forward the runner's
live, in-place-mutated message list rather than a snapshot taken once?) and
AgentNodeRunner's wiring of it (is it always composed via CompositeHook, even
when nobody is watching progress, so a failing persist can never abort the
node?).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.agent.hook import AgentHookContext, CompositeHook
from durin.agent.runner import AgentRunner, AgentRunResult
from durin.providers.base import LLMProvider
from durin.session.manager import SessionManager
from durin.workflow.engine import NodeExecutionError, NodeRunRequest
from durin.workflow.node_progress import NodeCheckpointHook
from durin.workflow.node_runner import AgentNodeRunner
from durin.workflow.spec import WorkNode

# ── NodeCheckpointHook: unit-level ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_after_iteration_reflects_the_live_list_not_a_stale_copy():
    """The real runner hands the hook the SAME list object every round, mutated
    in place (never replaced) — see AgentRunner.run's ``messages`` variable.
    Pin that the hook reads it fresh at call time: mutate the shared list
    between two rounds (two separate contexts, exactly how the runner drives
    it) and confirm the two persisted snapshots differ accordingly."""
    persisted = []
    hook = NodeCheckpointHook(lambda messages: persisted.append(list(messages)))

    live = [{"role": "user", "content": "hi"}]
    await hook.after_iteration(AgentHookContext(iteration=0, messages=live))

    live.append({"role": "assistant", "content": "round 2"})
    await hook.after_iteration(AgentHookContext(iteration=1, messages=live))

    assert persisted == [
        [{"role": "user", "content": "hi"}],
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "round 2"}],
    ]


@pytest.mark.asyncio
async def test_a_failing_persist_never_escapes_when_composed():
    """NodeCheckpointHook does not guard its own exceptions — it relies on
    always being wired inside a CompositeHook (see node_runner.py), whose
    per-hook error isolation is the actual safety net. Pin the contract at
    that composed boundary, the one production code relies on."""
    def _boom(messages):
        raise OSError("disk full")

    hook = CompositeHook([NodeCheckpointHook(_boom)])
    await hook.after_iteration(AgentHookContext(iteration=0, messages=[]))  # must not raise


# ── AgentNodeRunner wiring: integration-level ────────────────────────────────


def _fake_agent_runner(run) -> AgentRunner:
    provider = MagicMock(spec=LLMProvider)
    ar = AgentRunner(provider)
    ar.run = run
    return ar


def _req(**overrides):
    kw = dict(
        node=WorkNode(id="a", prompt="do it", next=None),
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    )
    kw.update(overrides)
    return NodeRunRequest(**kw)


def test_node_runner_checkpoints_even_without_a_progress_watcher(tmp_path):
    """Checkpointing must not be gated behind req.progress — a node's session
    is durable whether or not anything is watching its live progress."""
    sessions = SessionManager(workspace=tmp_path)
    ar = _fake_agent_runner(AsyncMock(return_value=AgentRunResult(final_content="ok", messages=[])))
    nr = AgentNodeRunner(ar, sessions, default_model="m")

    nr(_req())   # req.progress defaults to None
    spec = ar.run.call_args.args[0]
    assert spec.hook is not None

    asyncio.run(spec.hook.after_iteration(
        AgentHookContext(iteration=0, messages=[{"role": "user", "content": "checkpoint me"}])
    ))
    reloaded = SessionManager(workspace=tmp_path).get_or_create("workflow:r1:a:1")
    assert any(m.get("content") == "checkpoint me" for m in reloaded.messages)


def test_node_runner_checkpoint_tracks_the_live_conversation_across_rounds(tmp_path):
    """The historical bug this task fixes: a checkpoint closing over
    node_runner.py's own copy of the messages (built before the agent turn
    starts) would re-persist that SAME initial turn forever, because the real
    AgentRunner.run copies initial_messages once and then mutates its OWN list
    in place. Simulate that exact copy-then-mutate shape and confirm the
    round-1 checkpoint carries only round-1 content — proof the hook tracks
    the runner's live list, not a separate, stale one."""
    sessions = SessionManager(workspace=tmp_path)

    async def fake_run(spec):
        live = list(spec.initial_messages)   # mirrors AgentRunner.run's own copy
        live.append({"role": "assistant", "content": "round 1 output"})
        await spec.hook.after_iteration(AgentHookContext(iteration=0, messages=live))
        live.append({"role": "assistant", "content": "round 2 output"})
        await spec.hook.after_iteration(AgentHookContext(iteration=1, messages=live))
        return AgentRunResult(final_content="done", messages=live, stop_reason="completed")

    ar = _fake_agent_runner(AsyncMock(side_effect=fake_run))
    nr = AgentNodeRunner(ar, sessions, default_model="m")

    with patch.object(sessions, "save", wraps=sessions.save) as save_spy:
        nr(_req())

    node_saves = [c.args[0] for c in save_spy.call_args_list if c.args[0].key == "workflow:r1:a:1"]
    assert len(node_saves) >= 2, "expected at least one mid-node checkpoint plus the final persist"
    first = node_saves[0].messages
    assert any(m.get("content") == "round 1 output" for m in first)
    assert not any(m.get("content") == "round 2 output" for m in first), (
        "round-1 checkpoint must not already contain round-2 content"
    )
    assert any(m.get("content") == "round 2 output" for m in node_saves[-1].messages)


def test_node_runner_checkpoint_failure_does_not_abort_the_node(tmp_path):
    """Best-effort durability: a checkpoint persist failure (e.g. a disk error
    mid-turn) must never surface as, or cause, a node failure."""
    sessions = SessionManager(workspace=tmp_path)
    ar = _fake_agent_runner(AsyncMock(return_value=AgentRunResult(final_content="ok", messages=[])))
    nr = AgentNodeRunner(ar, sessions, default_model="m")

    nr(_req())
    spec = ar.run.call_args.args[0]

    with patch.object(nr, "_persist", side_effect=OSError("disk full")):
        asyncio.run(spec.hook.after_iteration(AgentHookContext(iteration=0, messages=[])))  # must not raise


def test_node_runner_composes_progress_and_checkpoint_hooks_together(tmp_path):
    """Task 4 wired NodeProgressHook; task 5 must not crowd it out — both must
    fire from the single composed hook the node's agent turn receives."""
    sessions = SessionManager(workspace=tmp_path)
    ar = _fake_agent_runner(AsyncMock(return_value=AgentRunResult(final_content="ok", messages=[])))
    nr = AgentNodeRunner(ar, sessions, default_model="m")
    progress_frames = []

    nr(_req(progress=progress_frames.append))
    spec = ar.run.call_args.args[0]

    asyncio.run(spec.hook.after_iteration(
        AgentHookContext(iteration=0, messages=[{"role": "user", "content": "x"}])
    ))

    assert progress_frames, "the progress hook must still fire when composed with the checkpoint hook"
    reloaded = SessionManager(workspace=tmp_path).get_or_create("workflow:r1:a:1")
    assert any(m.get("content") == "x" for m in reloaded.messages), "the checkpoint hook must still fire too"


# ── Failure path: must not erase what the checkpoint already saved ──────────


def test_node_runner_failure_after_checkpoint_persists_the_checkpointed_rounds(tmp_path):
    """The bug this task fixes: when the agent turn raises AFTER the checkpoint
    hook already saved a round, the node's session must end up holding that
    round — not the pre-turn snapshot. Before the fix, the except handler always
    persisted its own `messages` (never mutated in place, since AgentRunner.run
    copies initial_messages into its own list and mutates only that copy),
    silently overwriting the checkpoint's round-1 content with zero rounds of
    progress — exactly when someone wants to inspect why the node failed."""
    sessions = SessionManager(workspace=tmp_path)

    async def fake_run(spec):
        live = list(spec.initial_messages)   # mirrors AgentRunner.run's own copy
        live.append({"role": "assistant", "content": "round 1 output"})
        await spec.hook.after_iteration(AgentHookContext(iteration=0, messages=live))
        raise RuntimeError("provider exploded on round 2")

    ar = _fake_agent_runner(AsyncMock(side_effect=fake_run))
    nr = AgentNodeRunner(ar, sessions, default_model="m")

    with pytest.raises(NodeExecutionError):
        nr(_req())

    reloaded = SessionManager(workspace=tmp_path).get_or_create("workflow:r1:a:1")
    assert any(m.get("content") == "round 1 output" for m in reloaded.messages), (
        "the round-1 checkpoint must survive the failure, not be overwritten by "
        "the pre-turn snapshot"
    )


def test_node_runner_failure_before_any_checkpoint_persists_the_pre_turn_snapshot(tmp_path):
    """Degenerate case: the turn raises before round 1 even completes, so the
    checkpoint hook never fires and its `last_persisted` stays None. Must fall
    back to the pre-turn snapshot exactly as before this fix — unchanged
    behavior, not a crash from reading a hook that never ran."""
    sessions = SessionManager(workspace=tmp_path)

    async def fake_run(spec):
        raise RuntimeError("provider exploded before round 1")

    ar = _fake_agent_runner(AsyncMock(side_effect=fake_run))
    nr = AgentNodeRunner(ar, sessions, default_model="m")

    with pytest.raises(NodeExecutionError):
        nr(_req())

    pre_turn_messages = list(ar.run.call_args.args[0].initial_messages)
    reloaded = SessionManager(workspace=tmp_path).get_or_create("workflow:r1:a:1")
    assert reloaded.messages == pre_turn_messages
