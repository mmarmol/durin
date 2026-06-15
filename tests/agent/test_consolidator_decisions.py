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
