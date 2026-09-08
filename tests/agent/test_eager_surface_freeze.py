"""The eager memory surface freezes for the life of a session.

The pinned memory block and the hot layer sit in the stable tier of the
system prompt, so re-rendering them from disk on every turn hands the
provider a different prefix the moment anything writes an entity page. These
tests drive a real ``AgentLoop`` over a real workspace: an entity written
between two turns must not move either block, and turning the freeze off must
bring the write straight back into the hot layer.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.config.schema import MemoryEagerSurfaceConfig
from durin.memory.eager_surface import SNAPSHOT_KEY, EagerSnapshot
from durin.memory.field_patch import FieldPatch
from durin.memory.fts_index import FTSIndex
from durin.memory.memory_writer import write_entity
from durin.providers.base import LLMResponse
from durin.session.manager import Session, SessionManager

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
QUESTION = "What do you remember about the bakery on Main Street?"


class _FakeSearch:
    """The search backend at the process boundary; every turn gets no hits,
    so the automatic prefetch adds nothing to the message and the stable tier
    is the only thing under test."""

    name = "memory_search"
    description = "test double"
    parameters: dict = {}
    # Not a real Tool subclass, so the runner's own default (read_only=False
    # → concurrency_safe=False) needs restating here for tests that dispatch
    # an actual model-issued tool_call against this double.
    concurrency_safe = False

    async def execute(self, **kwargs):
        return {"total": 0, "sectioned_rendered": ""}

    def cast_params(self, params: dict) -> dict:
        # ToolRegistry.prepare_call casts before dispatching a real
        # model-issued tool_call; an empty schema means nothing to cast.
        return params

    def validate_params(self, params: dict) -> list:
        return []

    def to_schema(self) -> dict:
        # The post-save consolidation estimate reads every registered tool's
        # schema on a background task; without this it logs a spurious error.
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters,
        }}


def _write_entity(workspace: Path, ref: str, text: str, name: str) -> None:
    write_entity(
        workspace, ref,
        [FieldPatch(kind="body_append", value=text, author="agent", source_ref="s", at=NOW)],
        create=True, name=name,
    )


def _seed_workspace(workspace: Path) -> None:
    """A workspace the eager surface actually renders from: the principal the
    pinned block resolves for an unconfigured owner, plus a canonical page for
    the hot layer."""
    _write_entity(workspace, "person:anonymous", "Runs a small bakery supply business.", "Anon")
    _write_entity(workspace, "company:supplier", "Supplies flour to bakeries on Main St.", "Supplier")


def _make_loop(tmp_path: Path) -> tuple[AgentLoop, list[dict[str, str]]]:
    with FTSIndex.open(tmp_path):  # a real index file → the prefetch gate is open
        pass
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.tools.register(_FakeSearch())
    stable: list[dict[str, str]] = []

    async def _chat(*args, **kwargs):
        # Copied here rather than after the turn: the breakdown is per build,
        # and the post-save consolidation probe rebuilds it in the background.
        stable.append(dict(loop.context._last_layer_breakdown["stable"]))
        return LLMResponse(content="ok", tool_calls=[])

    loop.provider.chat_with_retry = AsyncMock(side_effect=_chat)
    return loop, stable


async def _two_turns_with_a_write(loop: AgentLoop, workspace: Path, chat_id: str = "c") -> None:
    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id=chat_id, content=QUESTION)
    )
    _write_entity(workspace, "company:bakery", "The bakery opened on Main St in March.", "Bakery")
    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id=chat_id, content=QUESTION)
    )


@pytest.mark.asyncio
async def test_a_write_between_turns_moves_neither_memory_block(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    loop, stable = _make_loop(tmp_path)

    await _two_turns_with_a_write(loop, tmp_path)

    assert len(stable) == 2
    assert stable[0]["memory_hot"], "the seeded workspace must render a hot layer"
    assert stable[0]["memory_pinned"], "the seeded principal must render a pinned block"
    assert stable[0]["memory_hot"] == stable[1]["memory_hot"]
    assert stable[0]["memory_pinned"] == stable[1]["memory_pinned"]
    assert "company:bakery" not in stable[1]["memory_hot"]
    # The whole stable-tier breakdown, not just the two frozen blocks: nothing
    # else about this turn's build (identity, bootstrap, SOUL, skills) moved
    # either, since only an entity page was written between the two turns.
    assert "".join(stable[0].values()) == "".join(stable[1].values())


@pytest.mark.asyncio
async def test_a_system_message_build_reuses_the_frozen_surface(tmp_path: Path) -> None:
    """A background subagent/workflow result lands on the user's own session
    key through ``_process_system_message`` — a second entry point into the
    prompt build, separate from the normal turn state machine. It must
    resolve the same stored snapshot BUILD would, or this build renders live
    and punctures the freeze the user's own turns keep."""
    _seed_workspace(tmp_path)
    loop, stable = _make_loop(tmp_path)

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )
    _write_entity(tmp_path, "company:bakery", "The bakery opened on Main St in March.", "Bakery")

    # Mirrors RunWorkflowTool._inject_result's real construction: a system
    # message routed back into the parent session via session_key_override.
    await loop._process_message(
        InboundMessage(
            channel="system",
            sender_id="workflow_background",
            chat_id="websocket:c",
            content="[Background workflow 'qa' finished]\n\nWorkflow run r1: completed\nFinal output:\n42",
            session_key_override="websocket:c",
            metadata={"injected_event": "workflow_background_result", "workflow": "qa"},
        )
    )

    assert len(stable) == 2
    assert stable[1]["memory_hot"] == stable[0]["memory_hot"]
    assert stable[1]["memory_pinned"] == stable[0]["memory_pinned"]
    assert "company:bakery" not in stable[1]["memory_hot"]


@pytest.mark.asyncio
async def test_a_system_message_build_binds_the_snapshot_for_its_own_tool_loop(
    tmp_path: Path,
) -> None:
    """``_process_system_message`` runs its own tool loop (``_run_agent_loop``),
    so a ``memory_search`` call made there owes the dedup the same bound
    surface BUILD's own turns get. Instrumented the same way as the
    prefetch's own binding test: record what the ContextVar carries at the
    moment the tool actually runs, inside the system-message entry point's
    tool loop specifically."""
    from durin.agent.tools.memory_search import _turn_eager_surface
    from durin.providers.base import ToolCallRequest

    _seed_workspace(tmp_path)
    loop, _ = _make_loop(tmp_path)

    seen: list[EagerSnapshot | None] = []
    fake = loop.tools.get("memory_search")
    original_execute = fake.execute

    async def _recording_execute(**kwargs):
        seen.append(_turn_eager_surface.get())
        return await original_execute(**kwargs)

    fake.execute = _recording_execute

    calls = {"n": 0}

    async def _chat(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            # The system message's own tool loop: ask for a memory_search
            # call so _recording_execute runs inside it.
            return LLMResponse(content="", tool_calls=[ToolCallRequest(
                id="c1", name="memory_search",
                arguments={"query": "bakery", "scope": "dreamed", "level": "warm"},
            )])
        return LLMResponse(content="ok", tool_calls=[])

    loop.provider.chat_with_retry = AsyncMock(side_effect=_chat)

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )
    session = loop.sessions.get_or_create("websocket:c")
    stored = EagerSnapshot.from_metadata(session.metadata[SNAPSHOT_KEY])
    assert stored is not None

    await loop._process_message(
        InboundMessage(
            channel="system",
            sender_id="workflow_background",
            chat_id="websocket:c",
            content="[Background workflow 'qa' finished]\n\nWorkflow run r1: completed",
            session_key_override="websocket:c",
            metadata={"injected_event": "workflow_background_result", "workflow": "qa"},
        )
    )

    # Turn 1's own prefetch call (bound to None: nothing stored yet on the
    # first build) is call index 0; the system message's tool-loop search is
    # index 1 — the one this test is actually about.
    assert len(seen) == 2
    assert seen[0] is None
    assert seen[1] == stored


@pytest.mark.asyncio
async def test_freeze_off_lets_the_write_reach_the_next_hot_layer(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    loop, stable = _make_loop(tmp_path)
    loop.app_config = SimpleNamespace(
        memory=SimpleNamespace(eager_surface=MemoryEagerSurfaceConfig(freeze=False))
    )

    await _two_turns_with_a_write(loop, tmp_path)

    assert stable[0]["memory_hot"] != stable[1]["memory_hot"]
    assert "company:bakery" in stable[1]["memory_hot"]
    session = loop.sessions.get_or_create("websocket:c")
    assert SNAPSHOT_KEY not in session.metadata


@pytest.mark.asyncio
async def test_turning_the_freeze_off_forgets_the_stored_surface(tmp_path: Path) -> None:
    """A snapshot must not outlive the setting that made it: with the default
    ``refresh_after_min`` it never ages out, so a later toggle back on would
    otherwise resurrect text rendered arbitrarily long ago."""
    _seed_workspace(tmp_path)
    loop, stable = _make_loop(tmp_path)

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )
    session = loop.sessions.get_or_create("websocket:c")
    assert SNAPSHOT_KEY in session.metadata

    loop.app_config = SimpleNamespace(
        memory=SimpleNamespace(eager_surface=MemoryEagerSurfaceConfig(freeze=False))
    )
    _write_entity(tmp_path, "company:bakery", "The bakery opened on Main St in March.", "Bakery")
    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )

    assert "company:bakery" in stable[1]["memory_hot"]
    assert SNAPSHOT_KEY not in session.metadata


@pytest.mark.asyncio
async def test_the_snapshot_is_stored_and_survives_a_save_load_round_trip(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    loop, stable = _make_loop(tmp_path)

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )

    session = loop.sessions.get_or_create("websocket:c")
    snap = EagerSnapshot.from_metadata(session.metadata.get(SNAPSHOT_KEY))
    assert snap is not None
    assert snap.pinned == stable[0]["memory_pinned"]
    assert snap.hot == stable[0]["memory_hot"]
    assert "person:anonymous" in snap.refs
    # 1-based message ordinal, the repo's turn numbering: the first build of a
    # session freezes at the turn its first message occupies.
    assert snap.turn == 1

    # A fresh manager over the same workspace: the snapshot rides the sidecar.
    reloaded = SessionManager(workspace=tmp_path).get_or_create("websocket:c")
    assert EagerSnapshot.from_metadata(reloaded.metadata.get(SNAPSHOT_KEY)) == snap


@pytest.mark.asyncio
async def test_autonomous_sessions_keep_rendering_live(tmp_path: Path) -> None:
    """Cron, workflow-node and subagent turns have no eager surface to freeze:
    each is its own short-lived session, so a snapshot would only cost a save."""
    _seed_workspace(tmp_path)
    loop, stable = _make_loop(tmp_path)

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION),
        session_key="cron:job1:run:1",
    )
    _write_entity(tmp_path, "company:bakery", "The bakery opened on Main St in March.", "Bakery")
    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION),
        session_key="cron:job1:run:1",
    )

    assert "company:bakery" in stable[1]["memory_hot"]
    assert SNAPSHOT_KEY not in loop.sessions.get_or_create("cron:job1:run:1").metadata


@pytest.mark.asyncio
async def test_a_workflow_node_session_keeps_rendering_live(tmp_path: Path) -> None:
    _seed_workspace(tmp_path)
    loop, stable = _make_loop(tmp_path)
    session = loop.sessions.get_or_create("websocket:node")
    session.metadata["origin_type"] = "workflow_node"
    loop.sessions.save(session)

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="node", content=QUESTION)
    )
    _write_entity(tmp_path, "company:bakery", "The bakery opened on Main St in March.", "Bakery")
    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="node", content=QUESTION)
    )

    assert "company:bakery" in stable[1]["memory_hot"]
    assert SNAPSHOT_KEY not in session.metadata


@pytest.mark.asyncio
async def test_refresh_window_drops_the_snapshot(tmp_path: Path) -> None:
    """``refresh_after_min`` above 0 re-renders once the snapshot is older than
    the window — the operator's lever for fresher eager memory."""
    _seed_workspace(tmp_path)
    loop, stable = _make_loop(tmp_path)
    loop.app_config = SimpleNamespace(
        memory=SimpleNamespace(eager_surface=MemoryEagerSurfaceConfig(refresh_after_min=30))
    )

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )
    # Age the snapshot past the window in place, the way wall-clock time would.
    session = loop.sessions.get_or_create("websocket:c")
    aged = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    session.metadata[SNAPSHOT_KEY] = {
        **session.metadata[SNAPSHOT_KEY], "frozen_at": aged.isoformat(),
    }
    _write_entity(tmp_path, "company:bakery", "The bakery opened on Main St in March.", "Bakery")
    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )

    assert "company:bakery" in stable[1]["memory_hot"]
    fresh = EagerSnapshot.from_metadata(session.metadata[SNAPSHOT_KEY])
    assert fresh is not None and fresh.frozen_at != aged.isoformat()


def test_session_is_autonomous_matches_origin_type_and_prefixes(tmp_path: Path) -> None:
    """One predicate backs both gates that must skip exactly the same
    sessions: the eager-surface freeze (``_eager_surface_freezes``) and the
    memory prefetch (its ``non_interactive`` reason in ``_memory_prefetch``).
    A workflow-node/subagent session is named by ``origin_type`` metadata;
    the runtime's autonomous kinds (cron, automation, dream…) are named by
    ``AUTONOMOUS_SESSION_PREFIXES``; anything else is attended and interactive."""
    loop, _ = _make_loop(tmp_path)

    interactive = Session(key="websocket:c")
    assert loop._session_is_autonomous(interactive, "websocket:c") is False

    workflow_node = Session(key="websocket:node")
    workflow_node.metadata["origin_type"] = "workflow_node"
    assert loop._session_is_autonomous(workflow_node, "websocket:node") is True

    assert loop._session_is_autonomous(Session(key="cron:job1:run:1"), "cron:job1:run:1") is True
    # No session object at all (a turn that has not fetched one yet) still
    # reads the session-kind half of the check off the key alone.
    assert loop._session_is_autonomous(None, "cron:job1:run:1") is True
    assert loop._session_is_autonomous(None, "websocket:c") is False


# ---------------------------------------------------------------------------
# Session boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_makes_the_next_turn_render_live_and_freeze_again(tmp_path: Path) -> None:
    """``/new`` starts a different conversation: the surface it inherits was
    rendered for the one that just closed, so the first turn after it renders
    from disk and stores a snapshot of its own."""
    _seed_workspace(tmp_path)
    loop, stable = _make_loop(tmp_path)

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )
    first = EagerSnapshot.from_metadata(
        loop.sessions.get_or_create("websocket:c").metadata[SNAPSHOT_KEY]
    )

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content="/new")
    )
    assert SNAPSHOT_KEY not in loop.sessions.get_or_create("websocket:c").metadata

    _write_entity(tmp_path, "company:bakery", "The bakery opened on Main St in March.", "Bakery")
    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )

    # A command never reaches the model, so these are the two model turns.
    assert len(stable) == 2
    assert "company:bakery" in stable[1]["memory_hot"]
    fresh = EagerSnapshot.from_metadata(
        loop.sessions.get_or_create("websocket:c").metadata[SNAPSHOT_KEY]
    )
    assert fresh is not None and first is not None
    assert fresh.hot == stable[1]["memory_hot"] != first.hot


@pytest.mark.asyncio
async def test_a_compaction_round_makes_the_next_turn_render_live(tmp_path: Path) -> None:
    """Compaction rewrites the conversation, so the cached prefix is gone
    anyway: refreshing the eager surface there is free, and it is the boundary
    that keeps a long session's eager view from going arbitrarily stale."""
    _seed_workspace(tmp_path)
    loop, stable = _make_loop(tmp_path)
    # The hook's own LLM work (decision log, learnings) is not under test.
    loop.consolidator.decision_log_enabled = False
    loop.consolidator.compaction_learnings_enabled = False

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )
    _write_entity(tmp_path, "company:bakery", "The bakery opened on Main St in March.", "Bakery")

    session = loop.sessions.get_or_create("websocket:c")
    assert SNAPSHOT_KEY in session.metadata
    session.last_consolidated = len(session.messages)
    await loop.consolidator._post_compaction_hooks(session, 0, True)
    assert SNAPSHOT_KEY not in session.metadata

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )

    assert stable[1]["memory_hot"] != stable[0]["memory_hot"]
    assert "company:bakery" in stable[1]["memory_hot"]
    assert SNAPSHOT_KEY in loop.sessions.get_or_create("websocket:c").metadata


@pytest.mark.asyncio
async def test_the_compaction_drop_is_saved_immediately(tmp_path: Path) -> None:
    """A turn that dies before its own save must not leave the stale surface on
    disk for the next process to pick up."""
    _seed_workspace(tmp_path)
    loop, _ = _make_loop(tmp_path)
    loop.consolidator.decision_log_enabled = False
    loop.consolidator.compaction_learnings_enabled = False

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )
    session = loop.sessions.get_or_create("websocket:c")
    session.last_consolidated = len(session.messages)
    await loop.consolidator._post_compaction_hooks(session, 0, True)

    reloaded = SessionManager(workspace=tmp_path).get_or_create("websocket:c")
    assert SNAPSHOT_KEY not in reloaded.metadata


# ---------------------------------------------------------------------------
# The turn's own searches are judged against the frozen surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_search_in_the_turn_is_judged_against_the_frozen_surface(
    tmp_path: Path,
) -> None:
    """An entity written after the freeze is in the workspace's hot layer but
    not in the prompt the model holds. The model's own search for it must
    render it whole: collapsing it to a pointer would cite content this
    conversation was never shown."""
    from durin.agent.tools.memory_search import MemorySearchTool
    from durin.providers.base import ToolCallRequest

    _seed_workspace(tmp_path)
    loop, _ = _make_loop(tmp_path)
    loop.tools.register(MemorySearchTool(workspace=tmp_path, context_dedup=True))

    tool_outputs: list[str] = []
    calls = {"n": 0}

    async def _chat(*args, **kwargs):
        messages = args[0] if args else kwargs["messages"]
        for m in messages:
            if m.get("role") == "tool":
                tool_outputs.append(str(m.get("content")))
        calls["n"] += 1
        if calls["n"] % 2 == 1:
            return LLMResponse(content="", tool_calls=[ToolCallRequest(
                id=f"c{calls['n']}", name="memory_search",
                arguments={"query": "bakery", "scope": "dreamed", "level": "warm"},
            )])
        return LLMResponse(content="ok", tool_calls=[])

    loop.provider.chat_with_retry = AsyncMock(side_effect=_chat)

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )
    _write_entity(tmp_path, "company:bakery", "The bakery opened on Main St in March.", "Bakery")
    tool_outputs.clear()
    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )

    assert tool_outputs, "the turn's search must have produced a tool result"
    payload = json.loads(tool_outputs[-1])
    assert "memory/entity_page/company:bakery" not in payload.get("already_in_context", [])
    assert "=== CANONICAL: memory/entity_page/company:bakery" in payload["sectioned_rendered"]
    # The page seeded before the freeze IS in the text the model holds, so it
    # still collapses — the frozen surface is being judged, not ignored.
    assert "memory/entity_page/company:supplier" in payload["already_in_context"]

    # The control: judged against the live workspace — no turn bound — the same
    # search collapses the write too, which is what the freeze has to prevent.
    live = await MemorySearchTool(workspace=tmp_path, context_dedup=True).execute(
        query="bakery", scope="dreamed", level="warm",
    )
    assert "memory/entity_page/company:bakery" in live["already_in_context"]


@pytest.mark.asyncio
async def test_the_surface_binding_does_not_outlive_the_turn(tmp_path: Path) -> None:
    """The binding describes one prompt. Left standing it would judge the next
    session's searches against this session's text."""
    from durin.agent.tools.memory_search import _turn_eager_surface

    _seed_workspace(tmp_path)
    loop, _ = _make_loop(tmp_path)

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )

    assert _turn_eager_surface.get() is None


# ---------------------------------------------------------------------------
# The turn's own automatic prefetch is judged against the frozen surface too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_prefetch_sees_the_snapshot_bound(tmp_path: Path) -> None:
    """The automatic warm search (``_memory_prefetch``) is a ``memory_search``
    call like any other, run before the tool loop even starts. Binding the
    eager surface only after ``_state_build`` awaits the prefetch would leave
    the ContextVar unset for that call, so this asserts the bound value the
    prefetch's own ``execute`` sees, not just the one the model's later tool
    calls see."""
    from durin.agent.tools.memory_search import _turn_eager_surface

    _seed_workspace(tmp_path)
    loop, _ = _make_loop(tmp_path)

    seen: list[EagerSnapshot | None] = []
    fake = loop.tools.get("memory_search")
    original_execute = fake.execute

    async def _recording_execute(**kwargs):
        seen.append(_turn_eager_surface.get())
        return await original_execute(**kwargs)

    fake.execute = _recording_execute

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )
    _write_entity(tmp_path, "company:bakery", "The bakery opened on Main St in March.", "Bakery")
    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )

    assert len(seen) == 2
    # Turn 1: nothing stored yet — the first build's own resolve returns None.
    assert seen[0] is None
    # Turn 2: the snapshot turn 1 froze is resolved and bound BEFORE the
    # prefetch runs, not after — the fix under test.
    session = loop.sessions.get_or_create("websocket:c")
    stored = EagerSnapshot.from_metadata(session.metadata[SNAPSHOT_KEY])
    assert stored is not None
    assert seen[1] == stored


@pytest.mark.asyncio
async def test_the_prefetch_block_carries_a_post_freeze_write_whole(
    tmp_path: Path,
) -> None:
    """The prefetch block is what actually carries a mid-session write to the
    model (fenced into the wire copy of the user message). If the prefetch's
    own search dedups against the live workspace instead of the frozen
    surface, a page written after the freeze collapses to a pointer line
    there — the model is told to go drill for content it was never shown,
    because the pointer claims it is already in the (stale) hot layer.

    A dedicated minimal workspace (not ``_seed_workspace``): the second
    turn's query needs a lexical match specific enough to land the written
    page without competing against other seeded entities, since the test
    double has no embedding model to fall back on for a looser match."""
    from durin.agent.tools.memory_search import MemorySearchTool

    loop, _ = _make_loop(tmp_path)
    loop.tools.register(MemorySearchTool(workspace=tmp_path, context_dedup=True))
    _write_entity(tmp_path, "person:anonymous", "Runs a small consulting business.", "Anon")

    wire_messages: list[list[dict]] = []

    async def _chat(*args, **kwargs):
        messages = args[0] if args else kwargs["messages"]
        wire_messages.append(messages)
        return LLMResponse(content="ok", tool_calls=[])

    loop.provider.chat_with_retry = AsyncMock(side_effect=_chat)

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content="checking in for today")
    )
    _write_entity(
        tmp_path, "company:bakery",
        "Lighthouse Bakery opened on Main St in March.", "Lighthouse Bakery",
    )
    await loop._process_message(
        InboundMessage(
            channel="websocket", sender_id="u", chat_id="c",
            content="Lighthouse Bakery opened on Main St",
        )
    )

    assert len(wire_messages) == 2
    user_message = [m for m in wire_messages[1] if m.get("role") == "user"][-1]
    content = user_message["content"]
    text = content if isinstance(content, str) else json.dumps(content)
    assert "=== CANONICAL: memory/entity_page/company:bakery" in text
    assert "memory/entity_page/company:bakery" not in _pointer_uris(text)


def _pointer_uris(wire_text: str) -> list[str]:
    """The uris listed under the prefetch block's in-context pointer section,
    if it rendered one — the same "- <uri>" lines ``render_in_context_section``
    prints."""
    marker = "## Matches shown in your Memory sections"
    if marker not in wire_text:
        return []
    section = wire_text.split(marker, 1)[1]
    return re.findall(r"^- (\S+)", section, re.M)


# ---------------------------------------------------------------------------
# The consolidator's token probe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_token_probe_measures_the_frozen_surface(tmp_path: Path) -> None:
    """The probe decides when to compact. Sizing a live render while the real
    prompt ships a larger frozen one under-estimates the prompt and lets the
    trigger fire too late."""
    _seed_workspace(tmp_path)
    loop, _ = _make_loop(tmp_path)

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )
    session = loop.sessions.get_or_create("websocket:c")
    stored = EagerSnapshot.from_metadata(session.metadata[SNAPSHOT_KEY])
    assert stored is not None
    padding = "\n".join(f"- fact {i} about the bakery" for i in range(2000))
    session.metadata[SNAPSHOT_KEY] = {
        **stored.to_metadata(), "hot": f"{stored.hot}\n\n{padding}",
    }

    frozen_tokens, _src = loop.consolidator.estimate_session_prompt_tokens(session)
    session.metadata.pop(SNAPSHOT_KEY)
    live_tokens, _src2 = loop.consolidator.estimate_session_prompt_tokens(session)

    assert frozen_tokens > live_tokens + 1000


@pytest.mark.asyncio
async def test_a_system_message_build_forgets_the_surface_when_freeze_is_off(
    tmp_path: Path,
) -> None:
    """The other build entry point owes the same cleanup BUILD does: a stored
    surface that outlived the setting would come back the moment the setting
    did."""
    _seed_workspace(tmp_path)
    loop, stable = _make_loop(tmp_path)

    await loop._process_message(
        InboundMessage(channel="websocket", sender_id="u", chat_id="c", content=QUESTION)
    )
    session = loop.sessions.get_or_create("websocket:c")
    assert SNAPSHOT_KEY in session.metadata

    loop.app_config = SimpleNamespace(
        memory=SimpleNamespace(eager_surface=MemoryEagerSurfaceConfig(freeze=False))
    )
    _write_entity(tmp_path, "company:bakery", "The bakery opened on Main St in March.", "Bakery")
    await loop._process_message(
        InboundMessage(
            channel="system",
            sender_id="workflow_background",
            chat_id="websocket:c",
            content="[Background workflow 'qa' finished]\n\nWorkflow run r1: completed",
            session_key_override="websocket:c",
            metadata={"injected_event": "workflow_background_result", "workflow": "qa"},
        )
    )

    assert "company:bakery" in stable[1]["memory_hot"]
    assert SNAPSHOT_KEY not in session.metadata
