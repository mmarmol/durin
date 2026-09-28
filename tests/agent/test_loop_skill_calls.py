from unittest.mock import MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.agent.skill_usage import emit_skill_used, extract_skill_calls
from durin.bus.queue import MessageBus
from durin.providers.base import GenerationSettings, LLMResponse, ToolCallRequest


def _record(metadata, all_messages, save_skip):
    # EXACT expression used in loop._state_save (keep in sync).
    new = all_messages[save_skip:]
    calls = extract_skill_calls(new)
    if calls:
        metadata.setdefault("skill_calls", []).extend(calls)
        emit_skill_used(calls)


def _assistant_read(skill):
    return {"role": "assistant", "tool_calls": [
        {"function": {"name": "read_file",
                      "arguments": {"path": f"skills/{skill}/SKILL.md"}}}]}


def test_only_new_turn_messages_are_recorded_no_reaccumulation():
    md = {}
    # Turn 1: history empty. all_messages = [user, assistant(read X)]. save_skip=1
    t1 = [{"role": "user", "content": "do X"}, _assistant_read("git-helper")]
    _record(md, t1, save_skip=1)
    # Turn 2: prior turn is now history; all_messages carries it again + new turn.
    t2 = t1 + [{"role": "user", "content": "do Y"}, _assistant_read("deploy-flow")]
    # save_skip excludes everything from turn 1 (its 2 msgs) + the 1 base offset = 3
    _record(md, t2, save_skip=3)
    # `turn` is relative to each save-slice (the loop records only new turns),
    # so both read at slice-local turn 1. Persisted `turn` is unused by the
    # usage consumers (they count by skill/op); the hindsight skill-signal pass
    # recomputes extract_skill_calls over the full post-cursor window instead.
    assert md["skill_calls"] == [
        {"skill": "git-helper", "op": "read", "turn": 1},
        {"skill": "deploy-flow", "op": "read", "turn": 1},
    ]
    # git-helper recorded exactly once, NOT re-counted on turn 2.


def test_no_skill_calls_is_noop():
    md = {}
    _record(md, [{"role": "user", "content": "hi"}], save_skip=1)
    assert "skill_calls" not in md


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
        if calls["n"] == 1:
            return LLMResponse(content="", tool_calls=[ToolCallRequest(
                id="r1", name="read_file", arguments={"path": str(skill)})])
        return LLMResponse(content="ok", tool_calls=[])

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (0, "test-counter")
    provider.chat_with_retry = _chat
    provider.chat_stream_with_retry = _chat
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")
    loop._schedule_background = lambda coro: coro.close()  # type: ignore[method-assign]

    async def _keep(sess, *, replay_max_messages=None):
        return None

    loop.consolidator.maybe_consolidate_by_tokens = _keep  # type: ignore[method-assign]
    await loop.process_direct("use the git helper", session_key="cli:skills")

    assert "skill.used" in sink.events
