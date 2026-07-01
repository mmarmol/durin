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


def _loop(tmp_path):
    return AgentLoop(bus=MessageBus(), provider=_Stub(), workspace=tmp_path)


def test_schedule_reindex_coalesces(tmp_path):
    async def run():
        loop = _loop(tmp_path)
        calls = []

        def fake_reindex(key):
            calls.append(key)

        loop.sessions.reindex_session = fake_reindex
        # three rapid schedules for the same key collapse toward one drain pass
        loop._schedule_session_reindex("cli:x")
        loop._schedule_session_reindex("cli:x")
        loop._schedule_session_reindex("cli:x")
        # let the background drainer run
        for _ in range(5):
            await asyncio.sleep(0)
        await asyncio.gather(*loop._background_tasks, return_exceptions=True)
        assert calls and set(calls) == {"cli:x"}
        assert len(calls) <= 2  # coalesced, not one-per-schedule

    asyncio.run(run())
