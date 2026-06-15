"""Tests for Consolidator.extract_decisions (auto-extraction at compaction)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from durin.agent.memory import Consolidator


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
