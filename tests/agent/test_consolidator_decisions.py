"""Tests for Consolidator.extract_decisions and extract_learnings (auto-extraction at compaction)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.memory import Consolidator, MemoryStore
from durin.session.manager import Session


def _consolidator(llm_content):
    async def fake_chat(**kwargs):
        return SimpleNamespace(content=llm_content, finish_reason="stop")

    cons = Consolidator.__new__(Consolidator)
    cons.provider = SimpleNamespace(chat_with_retry=fake_chat)
    cons.model = "test-model"
    cons._truncate_to_token_budget = lambda text: text
    return cons


@pytest.mark.asyncio
async def test_extract_decisions_parses_bullets():
    cons = _consolidator("- chose X over Y because Z\n- found gotcha in parser")
    out = await cons.extract_decisions([{"role": "user", "content": "hi"}])
    assert out == ["chose X over Y because Z", "found gotcha in parser"]


@pytest.mark.asyncio
async def test_extract_decisions_none_marker_returns_empty():
    cons = _consolidator("(none)")
    assert await cons.extract_decisions([{"role": "user", "content": "hi"}]) == []


@pytest.mark.asyncio
async def test_extract_decisions_empty_input_returns_empty():
    cons = _consolidator("- whatever")
    assert await cons.extract_decisions([]) == []


@pytest.mark.asyncio
async def test_extract_decisions_swallows_llm_error():
    cons = _consolidator(None)

    async def boom(**kwargs):
        raise RuntimeError("provider down")

    cons.provider.chat_with_retry = boom
    assert await cons.extract_decisions([{"role": "user", "content": "hi"}]) == []


@pytest.mark.asyncio
async def test_extracted_decisions_write_to_metadata_with_caps():
    """The compaction call site contract: extract -> add_decision(source='auto'), capped."""
    from durin.session.decision_log import DECISION_LOG_KEY, add_decision, parse_decisions

    cons = _consolidator("- chose separate call\n- found ordering bug")
    cons.decision_log_enabled = True
    cons.decision_log_max_entries = 10
    cons.decision_log_max_chars = 1500

    session_meta: dict = {}
    span = [{"role": "assistant", "content": "did work and decided things"}]
    decisions = await cons.extract_decisions(span)
    for d in decisions:
        add_decision(
            session_meta, d, source="auto", ts="t",
            max_entries=cons.decision_log_max_entries,
            max_chars=cons.decision_log_max_chars,
        )

    stored = parse_decisions(session_meta[DECISION_LOG_KEY])
    assert [e["text"] for e in stored] == ["chose separate call", "found ordering bug"]
    assert all(e["source"] == "auto" for e in stored)


@pytest.mark.asyncio
async def test_maybe_consolidate_persists_extracted_decisions(tmp_path):
    """maybe_consolidate_by_tokens drives the real persist path: extract_decisions
    result is written via add_decision(source='auto') into session.metadata and
    saved via sessions.save — verifying the call-site wiring in memory.py."""
    from durin.session.decision_log import DECISION_LOG_KEY, parse_decisions

    store = MemoryStore(tmp_path)
    sessions = MagicMock()
    sessions.save = MagicMock()

    cons = Consolidator(
        store=store,
        provider=MagicMock(),
        model="test-model",
        sessions=sessions,
        context_window_tokens=1000,
        build_messages=MagicMock(return_value=[]),
        get_tool_definitions=MagicMock(return_value=[]),
        max_completion_tokens=100,
        decision_log_enabled=True,
        decision_log_max_entries=10,
        decision_log_max_chars=1500,
    )

    # Build a real Session with enough messages so a boundary exists.
    # Messages at indices 0 and 50 are "user" so pick_consolidation_boundary
    # can find a legal eviction boundary at index 50.
    session = Session(key="test:decisions")
    for i in range(70):
        role = "user" if i in {0, 50} else "assistant"
        session.add_message(role, f"m{i}")

    # Force the token estimate above the trigger so a consolidation round fires.
    # Two return values: first above trigger (forces round), second below target
    # (exits loop after one round).
    cons.estimate_session_prompt_tokens = MagicMock(
        side_effect=[(1200, "tiktoken"), (400, "tiktoken")]
    )

    # Stub archive to return a truthy summary (required for if last_summary: to fire).
    cons.archive = AsyncMock(return_value=("did work and decided things", {"entities": [], "topics": []}))

    # Stub extract_decisions on THIS instance to return the two decisions.
    # This overrides the shared fixture's [] stub and exercises the call-site
    # wiring (span → if decisions: → add_decision → sessions.save → telemetry).
    cons.extract_decisions = AsyncMock(return_value=["chose separate call", "found ordering bug"])

    await cons.maybe_consolidate_by_tokens(session)

    # The two decisions must have been written into the real metadata dict.
    assert DECISION_LOG_KEY in session.metadata, "decision log not written to session.metadata"
    stored = parse_decisions(session.metadata[DECISION_LOG_KEY])
    texts = [e["text"] for e in stored]
    assert "chose separate call" in texts
    assert "found ordering bug" in texts
    assert all(e["source"] == "auto" for e in stored)
    # sessions.save must have been called (once for the compaction round,
    # once for the decision log write).
    assert sessions.save.call_count >= 2


@pytest.mark.asyncio
async def test_compaction_backstop_writes_learning_entity(tmp_path):
    """extract_learnings result is written as a feedback entity at compaction.

    The compaction block calls extract_learnings(span), then writes each
    learning as an entity via write_entity — verifying the call-site wiring.
    """
    from durin.memory.entity_page import EntityPage

    store = MemoryStore(tmp_path)
    sessions = MagicMock()
    sessions.save = MagicMock()

    cons = Consolidator(
        store=store,
        provider=MagicMock(),
        model="test-model",
        sessions=sessions,
        context_window_tokens=1000,
        build_messages=MagicMock(return_value=[]),
        get_tool_definitions=MagicMock(return_value=[]),
        max_completion_tokens=100,
        decision_log_enabled=False,  # isolate the learnings block
    )

    # Build a Session with enough messages for a compaction boundary.
    session = Session(key="test:learnings")
    for i in range(70):
        role = "user" if i in {0, 50} else "assistant"
        session.add_message(role, f"m{i}")

    # Force one compaction round.
    cons.estimate_session_prompt_tokens = MagicMock(
        side_effect=[(1200, "tiktoken"), (400, "tiktoken")]
    )
    cons.archive = AsyncMock(
        return_value=("did work and stated preferences", {"entities": [], "topics": []})
    )

    # Stub extract_learnings to return one durable learning.
    async def fake_extract_learnings(span):
        return [
            {
                "ref": "feedback:spanish-replies",
                "name": "Reply in Spanish",
                "body": (
                    "User prefers replies in Spanish. "
                    "Why: works in Spanish. "
                    "How to apply: converse in Spanish, keep code English."
                ),
            }
        ]

    cons.extract_learnings = fake_extract_learnings

    await cons.maybe_consolidate_by_tokens(session)

    entity_path = tmp_path / "memory" / "entities" / "feedback" / "spanish-replies.md"
    page = EntityPage.from_file(entity_path)
    assert page is not None, "feedback entity file was not created"
    assert "Spanish" in (page.body or ""), "entity body does not contain expected content"
