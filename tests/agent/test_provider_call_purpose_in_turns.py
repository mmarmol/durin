"""A turn's ``provider.call`` rows are billed to the right purpose.

A plain turn is ``chat``. A turn run on behalf of a cron job is bound
``cron`` by the cron handler around ``process_direct``; the loop's own
per-turn binding inherits it instead of relabelling the calls as chat.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.providers.base import GenerationSettings, LLMProvider, LLMResponse
from durin.telemetry.logger import bind_telemetry, get_session_logger, reset_telemetry


class _StubProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="k", api_base="http://unit.test")
        self.generation = GenerationSettings(max_tokens=64)

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:  # noqa: ANN001
        return LLMResponse(content="ok", finish_reason="stop", usage={"prompt_tokens": 5})

    def get_default_model(self) -> str:
        return "stub-model"


def _loop(tmp_path: Path) -> AgentLoop:
    provider = _StubProvider()
    loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="stub-model")
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop._schedule_background = lambda coro: coro.close()  # type: ignore[method-assign]
    return loop


def _purposes(telemetry_dir: Path) -> list[str]:
    out: list[str] = []
    for path in sorted(telemetry_dir.glob("cli_test_*.jsonl")):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if row["type"] == "provider.call":
                out.append(row["data"]["purpose"])
    return out


@pytest.mark.asyncio
async def test_a_plain_turn_is_billed_as_chat(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DURIN_HOME", str(tmp_path / "home"))
    loop = _loop(tmp_path / "ws")
    await loop.process_direct("hello", session_key="cli:test")
    assert _purposes(tmp_path / "home" / "telemetry") == ["chat"]


@pytest.mark.asyncio
async def test_a_turn_under_a_cron_binding_stays_cron(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DURIN_HOME", str(tmp_path / "home"))
    loop = _loop(tmp_path / "ws")
    token = bind_telemetry(get_session_logger("cli:test"), purpose="cron")
    try:
        await loop.process_direct("hello", session_key="cli:test")
    finally:
        reset_telemetry(token)
    assert _purposes(tmp_path / "home" / "telemetry") == ["cron"]
