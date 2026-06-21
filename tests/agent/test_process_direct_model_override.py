"""Per-turn model_preset override on process_direct.

Verifies that passing model_preset= to process_direct threads the value into
_process_message without mutating the global self.model_preset.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus


def _make_agent(tmp_path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096, temperature=0.1, reasoning_effort=None)
    with (
        patch("durin.agent.loop.ContextBuilder"),
        patch("durin.agent.loop.SessionManager"),
        patch("durin.agent.loop.SubagentManager") as MockSubMgr,
    ):
        MockSubMgr.return_value.cancel_by_session = AsyncMock(return_value=0)
        return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)


@pytest.mark.asyncio
async def test_process_direct_model_override_does_not_mutate_global(tmp_path):
    agent = _make_agent(tmp_path)
    before = agent.model_preset
    captured = {}

    async def fake_process_message(msg, *, session_key, model_preset=None, **kw):
        captured["model_preset"] = model_preset
        return None

    agent._process_message = fake_process_message  # type: ignore
    await agent.process_direct("hi", model_preset="override-model")
    assert captured["model_preset"] == "override-model"
    assert agent.model_preset == before
