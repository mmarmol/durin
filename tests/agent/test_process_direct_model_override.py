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


def test_resolve_model_override_provider_model_pair(tmp_path):
    """A 'provider model' picker ref registers an ad-hoc preset and resolves a
    per-turn snapshot WITHOUT mutating global model state."""
    agent = _make_agent(tmp_path)
    before_preset = agent.model_preset
    fake_snap = SimpleNamespace(
        provider=MagicMock(), model="gemini-2.5-flash", context_window_tokens=1_000_000
    )
    with patch.object(agent, "_build_model_preset_snapshot", return_value=fake_snap) as mock_build:
        snap = agent._resolve_model_override("gemini gemini-2.5-flash")

    assert snap is fake_snap
    # ad-hoc preset registered on the named provider, keyed by model name
    assert agent.model_presets["gemini-2.5-flash"].provider == "gemini"
    mock_build.assert_called_once_with("gemini-2.5-flash")
    # global model state untouched
    assert agent.model_preset == before_preset


def test_resolve_model_override_unresolvable_returns_none(tmp_path):
    agent = _make_agent(tmp_path)
    with patch.object(agent, "_build_model_preset_snapshot", side_effect=KeyError("nope")):
        assert agent._resolve_model_override("totally-unknown-xyz") is None


def test_adhoc_preset_config_builds_for_provider_model():
    from durin.command.builtin import adhoc_preset_config

    cfg = adhoc_preset_config(None, "gemini", "gemini-2.5-flash")
    assert cfg.provider == "gemini"
    assert cfg.model == "gemini-2.5-flash"
    assert cfg.context_window_tokens > 0
    assert cfg.max_tokens > 0
