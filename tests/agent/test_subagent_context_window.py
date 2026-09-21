"""A spawned subagent runs under the same context window as the session
that spawned it, and its run is visible in telemetry.

Without ``context_window_tokens`` on the child's ``AgentRunSpec`` the runner
has no input budget: the mid-turn precheck is skipped, the history snip is a
no-op and the microcompact collapses every stale tool result on every
iteration. A long research child then ends in the provider's context-length
error instead of durin's own budget. The child's token usage lived only in
the in-memory ``SubagentStatus``; a ``subagent.run`` row now lands in the
parent session's telemetry file so "what did the children cost" is
answerable after the fact.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.agent.runner import AgentRunResult, AgentRunSpec
from durin.agent.subagent import SubagentManager
from durin.bus.queue import MessageBus
from durin.providers.base import LLMProvider
from durin.providers.factory import ProviderSnapshot
from durin.telemetry.logger import TelemetryLogger, bind_telemetry, reset_telemetry


def _provider() -> LLMProvider:
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test-model"
    return provider


def _manager(tmp_path: Path, **kwargs) -> SubagentManager:
    return SubagentManager(
        provider=_provider(),
        workspace=tmp_path,
        bus=MessageBus(),
        model="test-model",
        max_tool_result_chars=16_000,
        **kwargs,
    )


async def _spawn_and_capture(manager: SubagentManager, **result_kwargs) -> AgentRunSpec:
    """Spawn one child against a recording runner and return the spec it ran with."""
    specs: list[AgentRunSpec] = []

    async def _run(spec: AgentRunSpec) -> AgentRunResult:
        specs.append(spec)
        return AgentRunResult(
            final_content="done", messages=[], stop_reason="completed", **result_kwargs
        )

    manager.runner.run = _run
    await manager.spawn("look something up", session_key="cli:test")
    await asyncio.gather(*manager._running_tasks.values())
    assert len(specs) == 1
    return specs[0]


@pytest.mark.asyncio
async def test_child_spec_carries_the_managers_window(tmp_path: Path) -> None:
    manager = _manager(tmp_path, context_window_tokens=32_000, context_block_limit=7)
    spec = await _spawn_and_capture(manager)
    assert spec.context_window_tokens == 32_000
    assert spec.context_block_limit == 7


@pytest.mark.asyncio
async def test_set_provider_moves_the_window_with_the_model(tmp_path: Path) -> None:
    manager = _manager(tmp_path, context_window_tokens=32_000)
    manager.set_provider(_provider(), "bigger-model", context_window_tokens=200_000)
    spec = await _spawn_and_capture(manager)
    assert spec.model == "bigger-model"
    assert spec.context_window_tokens == 200_000


def test_loop_threads_its_window_into_the_manager(tmp_path: Path) -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(
        bus=MessageBus(), provider=provider, workspace=tmp_path,
        model="test-model", context_window_tokens=99_000, context_block_limit=5,
    )
    assert loop.subagents.context_window_tokens == 99_000
    assert loop.subagents.context_block_limit == 5

    swapped = MagicMock()
    swapped.get_default_model.return_value = "other"
    loop._apply_provider_snapshot(
        ProviderSnapshot(
            provider=swapped, model="other", context_window_tokens=128_000, signature=("other",),
        ),
        publish_update=False,
    )
    assert loop.subagents.model == "other"
    assert loop.subagents.context_window_tokens == 128_000


@pytest.mark.asyncio
async def test_subagent_run_is_recorded_in_the_parents_telemetry(tmp_path: Path) -> None:
    manager = _manager(tmp_path, context_window_tokens=32_000)
    telemetry = TelemetryLogger(tmp_path / "telemetry" / "cli_test.jsonl", session_key="cli:test")
    token = bind_telemetry(telemetry)
    try:
        await _spawn_and_capture(
            manager, usage={"prompt_tokens": 1_200, "completion_tokens": 30},
        )
    finally:
        reset_telemetry(token)

    rows = [json.loads(line) for line in telemetry.path.read_text().splitlines()]
    runs = [r for r in rows if r["type"] == "subagent.run"]
    assert len(runs) == 1
    data = runs[0]["data"]
    assert data["session_key"] == "cli:test"
    assert data["task_id"]
    assert data["model"] == "test-model"
    assert data["stop_reason"] == "completed"
    assert data["prompt_tokens"] == 1_200
    assert data["completion_tokens"] == 30
    assert data["context_window_tokens"] == 32_000
    assert data["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_subagent_run_row_survives_a_crashed_child(tmp_path: Path) -> None:
    manager = _manager(tmp_path, context_window_tokens=32_000)
    telemetry = TelemetryLogger(tmp_path / "telemetry" / "cli_test.jsonl", session_key="cli:test")

    async def _boom(spec: AgentRunSpec) -> AgentRunResult:
        raise RuntimeError("provider exploded")

    manager.runner.run = _boom
    token = bind_telemetry(telemetry)
    try:
        await manager.spawn("look something up", session_key="cli:test")
        await asyncio.gather(*manager._running_tasks.values())
    finally:
        reset_telemetry(token)

    rows = [json.loads(line) for line in telemetry.path.read_text().splitlines()]
    runs = [r for r in rows if r["type"] == "subagent.run"]
    assert len(runs) == 1
    assert runs[0]["data"]["stop_reason"] == "error"
    assert "provider exploded" in runs[0]["data"]["error"]


@pytest.mark.asyncio
async def test_spawn_reply_describes_mode_inheritance_not_a_fixed_explore_mode(tmp_path: Path) -> None:
    """The subagent runs in the parent session's mode (build → it can edit and
    exec; plan/explore → read-only). The reply used to claim it always runs
    read-only, which made the model refuse to delegate work it could delegate."""
    manager = _manager(tmp_path)

    async def _run(spec: AgentRunSpec) -> AgentRunResult:
        return AgentRunResult(final_content="ok", messages=[])

    manager.runner.run = _run
    reply = await manager.spawn("do something", session_key="cli:test")
    await asyncio.gather(*manager._running_tasks.values())
    assert "always run in EXPLORE MODE" not in reply
    assert "same mode" in reply
