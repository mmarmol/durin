"""The eager memory surface freezes for the life of a session.

The pinned memory block and the hot layer sit in the stable tier of the
system prompt, so re-rendering them from disk on every turn hands the
provider a different prefix the moment anything writes an entity page. These
tests drive a real ``AgentLoop`` over a real workspace: an entity written
between two turns must not move either block, and turning the freeze off must
bring the write straight back into the hot layer.
"""

from __future__ import annotations

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
from durin.session.manager import SessionManager

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)
QUESTION = "What do you remember about the bakery on Main Street?"


class _FakeSearch:
    """The search backend at the process boundary; every turn gets no hits,
    so the automatic prefetch adds nothing to the message and the stable tier
    is the only thing under test."""

    name = "memory_search"
    description = "test double"
    parameters: dict = {}

    async def execute(self, **kwargs):
        return {"total": 0, "sectioned_rendered": ""}

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
