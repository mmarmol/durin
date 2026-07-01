import asyncio

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.providers.base import LLMProvider


class _Stub(LLMProvider):
    def get_default_model(self):
        return "stub"

    async def chat(self, **kwargs):
        raise NotImplementedError

    async def chat_stream(self, **kwargs):
        raise NotImplementedError


def test_loop_builds_lane_and_ceiling(tmp_path):
    loop = AgentLoop(bus=MessageBus(), provider=_Stub(), workspace=tmp_path)
    assert loop._interactive_lane.limit >= 1
    assert loop._ceiling.limit >= loop._interactive_lane.limit
    # subagent manager shares the SAME ceiling object (so subagents count against it)
    assert loop.subagents._ceiling is loop._ceiling


def test_env_overrides_interactive_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("DURIN_MAX_CONCURRENT_REQUESTS", "7")
    loop = AgentLoop(bus=MessageBus(), provider=_Stub(), workspace=tmp_path)
    assert loop._interactive_lane.limit == 7
