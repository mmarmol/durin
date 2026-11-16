from unittest.mock import MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.providers.base import GenerationSettings, LLMResponse, ToolCallRequest


class _Sink:
    """A session telemetry logger that records event names."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def log(self, event, data=None, **kwargs) -> None:
        self.events.append(event)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.mark.asyncio
async def test_a_turn_that_reads_a_skill_records_skill_used(tmp_path, monkeypatch) -> None:
    """Through the real loop: the usage signal is written to the session's
    telemetry, not only to its metadata."""
    skill = tmp_path / "skills" / "git-helper" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: git-helper\ndescription: helps with git\n---\n# Git helper\n",
                     encoding="utf-8")
    sink = _Sink()
    monkeypatch.setattr("durin.telemetry.logger.get_session_logger",
                        lambda key, base_dir=None: sink)
    calls = {"n": 0}

    async def _chat(*args, messages=None, **kwargs):
        calls["n"] += 1
        # Each turn is two calls: one that reads the skill, one that answers.
        if calls["n"] % 2 == 1:
            return LLMResponse(content="", tool_calls=[ToolCallRequest(
                id=f"r{calls['n']}", name="read_file", arguments={"path": str(skill)})])
        return LLMResponse(content="ok", tool_calls=[])

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (0, "test-counter")
    provider.chat_with_retry = _chat
    provider.chat_stream_with_retry = _chat
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop._schedule_background = lambda coro: coro.close()  # type: ignore[method-assign]

    async def _keep(sess, *, replay_max_messages=None, force=False, limits=None, persona_soul=None):
        return None

    loop.consolidator.maybe_consolidate_by_tokens = _keep  # type: ignore[method-assign]
    await loop.process_direct("use the git helper", session_key="cli:skills")

    assert "skill.used" in sink.events

    await loop.process_direct("use it again", session_key="cli:skills")

    session = loop.sessions.get_or_create("cli:skills")
    recorded = session.metadata["skill_calls"]
    # One use per turn, never re-counted as the conversation accumulates, each
    # stamped with the place its message holds in the session.
    assert [c["skill"] for c in recorded] == ["git-helper", "git-helper"]
    for call in recorded:
        message = session.messages[call["turn"] - 1]
        assert message["role"] == "assistant" and message.get("tool_calls")
    assert recorded[0]["turn"] < recorded[1]["turn"]
