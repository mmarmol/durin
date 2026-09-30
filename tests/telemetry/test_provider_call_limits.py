"""``provider.call`` says what limits the call ran under.

A row carried the tokens a call spent but not the window it ran in, the
output cap it asked for, or the input budget its run allowed, so a judge
running on a 65,536 window with an 8,192 output cap looked like any other
call. Every row now records the ``max_tokens`` sent on the request, and a
call made by an agent run (or a workflow node's verdict / delivery call)
also records the run's ``context_window_tokens`` and ``input_budget_tokens``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.runner import AgentRunner, AgentRunSpec
from durin.providers.base import GenerationSettings, LLMProvider, LLMResponse, ToolCallRequest
from durin.telemetry.logger import (
    TelemetryLogger,
    bind_call_limits,
    bind_telemetry,
    current_call_limits,
    reset_call_limits,
    reset_telemetry,
)
from durin.telemetry.schema import ProviderCallEvent


class _StubProvider(LLMProvider):
    def __init__(self, max_tokens: int = 4096) -> None:
        super().__init__(api_key="k", api_base="http://unit.test")
        self.generation = GenerationSettings(max_tokens=max_tokens)

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:  # noqa: ANN001
        return LLMResponse(content="ok", finish_reason="stop", usage={"prompt_tokens": 3})

    def get_default_model(self) -> str:
        return "stub-model"


def _rows(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r["data"] for r in rows if r["type"] == "provider.call"]


def _logged(tmp_path, fn) -> list[dict]:
    log = TelemetryLogger(tmp_path / "t.jsonl", session_key="s")
    token = bind_telemetry(log)
    try:
        fn()
    finally:
        reset_telemetry(token)
    return _rows(log.path)


def test_the_limits_are_part_of_the_schema() -> None:
    for field in ("context_window_tokens", "max_tokens", "input_budget_tokens"):
        assert field in ProviderCallEvent.__annotations__
        # Present only when known (the schema module postpones annotations,
        # so the marker is read from the annotation, not __optional_keys__).
        assert "NotRequired" in repr(ProviderCallEvent.__annotations__[field])


def test_a_call_records_the_max_tokens_it_sent(tmp_path) -> None:
    p = _StubProvider()
    rows = _logged(tmp_path, lambda: asyncio.run(p.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}], model="m", max_tokens=1234,
    )))
    assert rows[0]["max_tokens"] == 1234


def test_a_call_that_names_no_max_tokens_records_the_providers(tmp_path) -> None:
    p = _StubProvider(max_tokens=131_072)
    rows = _logged(tmp_path, lambda: asyncio.run(p.chat_stream_with_retry(
        messages=[{"role": "user", "content": "hi"}], model="m",
    )))
    assert rows[0]["max_tokens"] == 131_072


def test_a_call_outside_a_run_records_no_window_or_budget(tmp_path) -> None:
    p = _StubProvider()
    rows = _logged(tmp_path, lambda: asyncio.run(p.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}], model="m",
    )))
    assert "context_window_tokens" not in rows[0]
    assert "input_budget_tokens" not in rows[0]


def test_the_bound_limits_tag_the_call_and_reset_restores_them(tmp_path) -> None:
    p = _StubProvider()

    def _calls() -> None:
        token = bind_call_limits(context_window_tokens=200_000, input_budget_tokens=166_208)
        try:
            assert current_call_limits() == {
                "context_window_tokens": 200_000, "input_budget_tokens": 166_208,
            }
            asyncio.run(p.chat_with_retry(messages=[{"role": "user", "content": "a"}], model="m"))
        finally:
            reset_call_limits(token)
        assert current_call_limits() is None
        asyncio.run(p.chat_with_retry(messages=[{"role": "user", "content": "b"}], model="m"))

    first, second = _logged(tmp_path, _calls)
    assert (first["context_window_tokens"], first["input_budget_tokens"]) == (200_000, 166_208)
    assert "context_window_tokens" not in second


class _Keyed(LLMProvider):
    """A provider under a registry name that answers with a fixed response."""

    def __init__(self, key: str, response: LLMResponse, max_tokens: int = 4096) -> None:
        super().__init__(api_key="k", api_base="http://unit.test")
        self.provider_key = key
        self.generation = GenerationSettings(max_tokens=max_tokens)
        self._response = response

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:  # noqa: ANN001
        return self._response

    def get_default_model(self) -> str:
        return "default"


def _failover(primary_response: LLMResponse):
    from durin.config.schema import ModelPresetConfig
    from durin.providers.fallback_provider import FallbackProvider

    fallback = _Keyed("openrouter", LLMResponse(content="ok", finish_reason="stop", usage={"prompt_tokens": 5}))
    return FallbackProvider(
        primary=_Keyed("zai_coding_plan", primary_response, max_tokens=8192),
        fallback_presets=[ModelPresetConfig(
            model="z-ai/glm-5-turbo", provider="openrouter", max_tokens=131_072, context_window_tokens=200_000,
        )],
        provider_factory=lambda _preset: fallback,
    )


def test_a_failover_records_the_provider_model_and_cap_the_fallback_was_sent(tmp_path) -> None:
    """The row used to name the primary, its model and its cap for a
    response the fallback produced."""
    overloaded = LLMResponse(content="overloaded", finish_reason="error", error_status_code=529)
    wrapper = _failover(overloaded)
    rows = _logged(tmp_path, lambda: asyncio.run(wrapper.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}], model="glm-5.3",
    )))
    [row] = rows
    assert (row["provider"], row["model"], row["max_tokens"]) == ("openrouter", "z-ai/glm-5-turbo", 131_072)
    assert row["finish_reason"] == "stop"


def test_a_call_the_primary_answers_records_the_primary(tmp_path) -> None:
    wrapper = _failover(LLMResponse(content="fine", finish_reason="stop"))
    [row] = _logged(tmp_path, lambda: asyncio.run(wrapper.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}], model="glm-5.3",
    )))
    assert (row["provider"], row["model"], row["max_tokens"]) == ("zai_coding_plan", "glm-5.3", 8192)


def test_an_explicit_emit_records_the_max_tokens_it_names(tmp_path) -> None:
    """The vision / audio bridges call chat() directly and emit their own row."""
    p = _StubProvider()
    rows = _logged(tmp_path, lambda: p.emit_call_telemetry(
        model="m", response=LLMResponse(content="", finish_reason="stop"),
        duration_ms=1.0, purpose="vision", max_tokens=2048,
    ))
    assert rows[0]["max_tokens"] == 2048


# --- an agent run records its own limits -----------------------------------------------


def _spec(provider, **kw) -> AgentRunSpec:
    tools = MagicMock()
    tools.get_definitions.return_value = []
    return AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hi"}],
        tools=tools, model="stub-model", provider=provider,
        max_iterations=1, max_tool_result_chars=16_000, **kw,
    )


@pytest.mark.asyncio
async def test_a_run_records_its_window_budget_and_the_dynamic_max_tokens(tmp_path) -> None:
    provider = _StubProvider(max_tokens=131_072)
    runner = AgentRunner(provider)
    log = TelemetryLogger(tmp_path / "t.jsonl", session_key="s")
    token = bind_telemetry(log)
    try:
        await runner.run(_spec(provider, context_window_tokens=200_000))
    finally:
        reset_telemetry(token)
    [row] = _rows(log.path)
    spec = _spec(provider, context_window_tokens=200_000)
    assert row["context_window_tokens"] == 200_000
    # The input budget holds back a capped output reservation, not the full
    # 131,072 ceiling.
    assert row["input_budget_tokens"] == runner._input_budget(spec, provider)
    assert row["input_budget_tokens"] == 200_000 - 32_768 - 1024
    # The request asked for the ceiling clamped to the room the prompt left.
    assert row["max_tokens"] == 131_072
    assert current_call_limits() is None


@pytest.mark.asyncio
async def test_a_run_near_the_window_records_the_shrunk_max_tokens(tmp_path) -> None:
    """A large prompt leaves less room than the output ceiling: the row
    records the smaller cap actually sent, not the model's ceiling."""
    provider = _StubProvider(max_tokens=131_072)
    runner = AgentRunner(provider)
    tools = MagicMock()
    tools.get_definitions.return_value = []
    spec = AgentRunSpec(
        initial_messages=[{"role": "user", "content": "hello " * 20_000}],
        tools=tools, model="stub-model", provider=provider,
        max_iterations=1, max_tool_result_chars=16_000, context_window_tokens=140_000,
    )
    log = TelemetryLogger(tmp_path / "t.jsonl", session_key="s")
    token = bind_telemetry(log)
    try:
        await runner.run(spec)
    finally:
        reset_telemetry(token)
    [row] = _rows(log.path)
    assert row["context_window_tokens"] == 140_000
    # The ~20K-token prompt plus the cap sent stays inside the window.
    assert 1 <= row["max_tokens"] <= 140_000 - 10_000 - 1024 < 131_072


@pytest.mark.asyncio
async def test_a_call_made_by_a_tool_during_a_run_does_not_carry_the_runs_limits(tmp_path) -> None:
    """A tool that calls another model (an aux bridge, a spawned child) runs
    between the run's requests; its row must not claim the run's window."""
    from durin.agent.tools.base import Tool
    from durin.agent.tools.registry import ToolRegistry

    aux = _StubProvider(max_tokens=512)

    class _AskAux(Tool):
        @property
        def name(self) -> str:
            return "ask_aux"

        @property
        def description(self) -> str:
            return "Ask another model."

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs) -> str:  # noqa: ANN003
            await aux.chat_with_retry(messages=[{"role": "user", "content": "x"}], model="aux-model")
            return "done"

    tools = ToolRegistry()
    tools.register(_AskAux())
    replies = iter([
        LLMResponse(
            content="", finish_reason="tool_calls",
            tool_calls=[ToolCallRequest(id="c1", name="ask_aux", arguments={})],
        ),
        LLMResponse(content="final", finish_reason="stop"),
    ])

    class _Primary(_StubProvider):
        async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:  # noqa: ANN001
            return next(replies)

    primary = _Primary(max_tokens=4096)
    runner = AgentRunner(primary)
    log = TelemetryLogger(tmp_path / "t.jsonl", session_key="s")
    token = bind_telemetry(log)
    try:
        await runner.run(AgentRunSpec(
            initial_messages=[{"role": "user", "content": "hi"}],
            tools=tools, model="stub-model", provider=primary,
            max_iterations=3, max_tool_result_chars=16_000, context_window_tokens=100_000,
        ))
    finally:
        reset_telemetry(token)
    rows = _rows(log.path)
    aux_rows = [r for r in rows if r["model"] == "aux-model"]
    run_rows = [r for r in rows if r["model"] == "stub-model"]
    assert len(run_rows) == 2 and all(r["context_window_tokens"] == 100_000 for r in run_rows)
    assert aux_rows and "context_window_tokens" not in aux_rows[0]
    assert aux_rows[0]["max_tokens"] == 512


# --- a workflow node's verdict call records the node's limits ---------------------------


def test_a_nodes_route_verdict_call_records_the_nodes_limits(tmp_path) -> None:
    from durin.agent.runner import AgentRunResult
    from durin.config.schema import Config
    from durin.session.manager import SessionManager
    from durin.workflow.engine import NodeRunRequest
    from durin.workflow.node_runner import AgentNodeRunner
    from durin.workflow.spec import WorkNode

    config = Config()
    config.agents.defaults.provider = "zai_coding_plan"
    config.agents.defaults.model = "my-local-model"
    config.agents.defaults.context_window_tokens = 64_000
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "my-local-model"
    provider.provider_key = "zai_coding_plan"
    provider.generation = GenerationSettings(max_tokens=8000)
    seen: list[dict | None] = []

    async def _verdict(**kwargs):
        seen.append(current_call_limits())
        return SimpleNamespace(tool_calls=[SimpleNamespace(name="route", arguments={"label": "PASS"})])

    provider.chat_with_retry = AsyncMock(side_effect=_verdict)
    runner = AgentRunner(provider)

    async def _run(spec):
        return AgentRunResult(final_content="looks fine", messages=list(spec.initial_messages))

    runner.run = AsyncMock(side_effect=_run)
    nr = AgentNodeRunner(runner, SessionManager(workspace=tmp_path), default_model="my-local-model", app_config=config)
    nr(NodeRunRequest(
        node=WorkNode(id="gate", prompt="Judge it.", cases={"PASS": None, "FAIL": None}),
        task="t", upstream_output=None, shared_context=[],
        run_id="r1", iteration=1, root_session_key=None,
    ))
    assert seen == [{"context_window_tokens": 64_000, "input_budget_tokens": 64_000 - 8000 - 1024}]
    assert current_call_limits() is None
