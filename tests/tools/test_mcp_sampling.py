"""Tests for SP-6 server→client capabilities (roots, logging, sampling).

Phases 6a (roots), 6b (logging), 6c (sampling config + runner), and
6d (wiring) pure-unit tests — no transport needed.
"""
from __future__ import annotations

import pytest

from durin.agent.tools.mcp_connection import MCPServerConnection
from durin.agent.tools.registry import ToolRegistry
from durin.config.schema import MCPServerConfig


def _conn(workspace="/tmp/ws", **cfg_kw):
    return MCPServerConnection(
        "s", MCPServerConfig(**cfg_kw), ToolRegistry(), workspace=workspace
    )


# ---------------------------------------------------------------------------
# 6a — roots
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_roots_returns_workspace_file_uri(tmp_path):
    conn = _conn(workspace=str(tmp_path))
    cb = conn._make_list_roots_callback()
    result = await cb(context=None)
    assert len(result.roots) == 1
    assert str(result.roots[0].uri).startswith("file://")
    # resolve() on macOS expands /tmp → /private/tmp; check via Path comparison
    from pathlib import Path
    assert Path(result.roots[0].uri.path) == tmp_path.resolve()
    assert result.roots[0].name == "workspace"


@pytest.mark.asyncio
async def test_list_roots_empty_when_no_workspace():
    conn = MCPServerConnection("s", MCPServerConfig(), ToolRegistry(), workspace=None)
    cb = conn._make_list_roots_callback()
    result = await cb(context=None)
    assert result.roots == []


def test_session_kwargs_includes_roots_callback():
    conn = _conn()
    kwargs = conn._session_kwargs()
    assert "list_roots_callback" in kwargs
    assert callable(kwargs["list_roots_callback"])


# ---------------------------------------------------------------------------
# 6b — logging
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mcp_level,loguru_level", [
    ("debug", "DEBUG"), ("info", "INFO"), ("notice", "INFO"),
    ("warning", "WARNING"), ("error", "ERROR"),
    ("critical", "CRITICAL"), ("alert", "CRITICAL"), ("emergency", "CRITICAL"),
])
def test_level_mapping(mcp_level, loguru_level):
    from durin.agent.tools.mcp_sampling import mcp_log_level_to_loguru
    assert mcp_log_level_to_loguru(mcp_level) == loguru_level


@pytest.mark.asyncio
async def test_logging_callback_routes_to_logger():
    import mcp.types as types

    from durin.agent.tools.mcp_sampling import mcp_log_level_to_loguru  # noqa: F401

    logged = []

    conn = _conn()
    cb = conn._make_logging_callback()

    # Intercept loguru output via a sink
    from loguru import logger
    sink_id = logger.add(lambda msg: logged.append(msg), level="DEBUG", format="{message}")
    try:
        params = types.LoggingMessageNotificationParams(
            level="error", logger="weather-server", data="upstream API failed"
        )
        await cb(params=params)
    finally:
        logger.remove(sink_id)

    assert any(
        "weather-server" in str(m) and "upstream API failed" in str(m)
        for m in logged
    ), f"Expected log not found. Got: {logged}"


def test_session_kwargs_includes_logging_callback():
    conn = _conn()
    assert "logging_callback" in conn._session_kwargs()


# ---------------------------------------------------------------------------
# 6c.1 — MCPSamplingConfig schema
# ---------------------------------------------------------------------------

from durin.config.schema import MCPSamplingConfig  # noqa: E402


def test_sampling_defaults():
    s = MCPSamplingConfig()
    assert s.enabled is False
    assert s.max_tokens_cap == 4096
    assert s.requests_per_minute == 10
    assert s.allowed_models == []
    assert s.allow_tools is True
    assert s.max_tool_rounds == 4
    assert s.model is None


def test_server_config_has_sampling():
    cfg = MCPServerConfig(command="npx")
    assert isinstance(cfg.sampling, MCPSamplingConfig)
    assert cfg.sampling.enabled is False


# ---------------------------------------------------------------------------
# 6c.2 — RpmLimiter
# ---------------------------------------------------------------------------


def test_rpm_limiter_allows_under_limit():
    from durin.agent.tools.mcp_sampling import RpmLimiter
    lim = RpmLimiter(rpm=3, now=lambda: 1000.0)
    assert lim.allow() and lim.allow() and lim.allow()
    assert not lim.allow()  # 4th within same minute


def test_rpm_limiter_recovers_after_window():
    from durin.agent.tools.mcp_sampling import RpmLimiter
    t = {"v": 1000.0}
    lim = RpmLimiter(rpm=2, now=lambda: t["v"])
    assert lim.allow() and lim.allow() and not lim.allow()
    t["v"] = 1061.0  # 61s later — window cleared
    assert lim.allow()


# ---------------------------------------------------------------------------
# 6c.3 — message + tool translators
# ---------------------------------------------------------------------------


def test_sampling_messages_to_openai_text():
    import mcp.types as types

    from durin.agent.tools.mcp_sampling import sampling_messages_to_openai

    msgs = [
        types.SamplingMessage(role="user", content=types.TextContent(type="text", text="hi")),
        types.SamplingMessage(role="assistant", content=types.TextContent(type="text", text="hello")),
    ]
    out = sampling_messages_to_openai(msgs, system_prompt="be terse")
    assert out[0] == {"role": "system", "content": "be terse"}
    assert out[1] == {"role": "user", "content": "hi"}
    assert out[2] == {"role": "assistant", "content": "hello"}


def test_sampling_image_message_becomes_image_block():
    import mcp.types as types

    from durin.agent.tools.mcp_sampling import sampling_messages_to_openai

    msgs = [types.SamplingMessage(
        role="user",
        content=types.ImageContent(type="image", data="QkFTRTY0", mimeType="image/png"),
    )]
    out = sampling_messages_to_openai(msgs, system_prompt=None)
    block = out[0]["content"][0]
    assert block["type"] == "image_url"
    assert block["image_url"]["url"].startswith("data:image/png;base64,")


def test_mcp_tool_to_openai_tool():
    import mcp.types as types

    from durin.agent.tools.mcp_sampling import mcp_tools_to_openai

    tools = [types.Tool(
        name="lookup", description="d", inputSchema={"type": "object", "properties": {}}
    )]
    out = mcp_tools_to_openai(tools)
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "lookup"


def test_llm_text_response_to_create_message_result():
    from durin.agent.tools.mcp_sampling import llm_response_to_sampling_result
    from durin.providers.base import LLMResponse

    res = llm_response_to_sampling_result(
        LLMResponse(content="the answer", finish_reason="stop"), model="m1", emit_tools=False
    )
    assert res.role == "assistant"
    assert res.content.text == "the answer"
    assert res.model == "m1"
    assert res.stopReason == "endTurn"


def test_llm_tool_calls_to_create_message_result_with_tools():

    from durin.agent.tools.mcp_sampling import llm_response_to_sampling_result
    from durin.providers.base import LLMResponse, ToolCallRequest

    res = llm_response_to_sampling_result(
        LLMResponse(
            content=None, finish_reason="tool_calls",
            tool_calls=[ToolCallRequest(id="c1", name="lookup", arguments={"q": "x"})],
        ),
        model="m1", emit_tools=True,
    )
    content = res.content if isinstance(res.content, list) else [res.content]
    tu = [b for b in content if getattr(b, "type", None) == "tool_use"]
    assert tu and tu[0].name == "lookup" and tu[0].input == {"q": "x"}
    assert res.stopReason == "toolUse"


# ---------------------------------------------------------------------------
# 6c.4 — SamplingGovernance + SamplingRunner
# ---------------------------------------------------------------------------

import mcp.types as _types  # noqa: E402

from durin.agent.tools.mcp_sampling import SamplingGovernance, SamplingRunner  # noqa: E402
from durin.providers.base import LLMResponse, ToolCallRequest  # noqa: E402


class _FakeProvider:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _sampling_params(max_tokens=100, tools=None):
    return _types.CreateMessageRequestParams(
        messages=[_types.SamplingMessage(
            role="user", content=_types.TextContent(type="text", text="q")
        )],
        maxTokens=max_tokens,
        tools=tools,
    )


def _runner(provider, **gov_kw):
    gov = SamplingGovernance(**{
        "max_tokens_cap": 4096, "requests_per_minute": 10,
        "allowed_models": [], "allow_tools": True, "max_tool_rounds": 2,
        **gov_kw,
    })
    return SamplingRunner(provider=provider, default_model="m-default", governance=gov)


@pytest.mark.asyncio
async def test_run_happy_path_returns_text_result():
    p = _FakeProvider(LLMResponse(content="answer", finish_reason="stop"))
    runner = _runner(p)
    res = await runner.run(_sampling_params())
    assert isinstance(res, _types.CreateMessageResult)
    assert res.content.text == "answer"
    assert res.model == "m-default"


@pytest.mark.asyncio
async def test_max_tokens_is_capped():
    p = _FakeProvider(LLMResponse(content="ok", finish_reason="stop"))
    runner = _runner(p, max_tokens_cap=50)
    await runner.run(_sampling_params(max_tokens=99999))
    assert p.calls[0]["max_tokens"] == 50


@pytest.mark.asyncio
async def test_rpm_limit_returns_error_data():
    p = _FakeProvider(LLMResponse(content="ok", finish_reason="stop"))
    runner = _runner(p, requests_per_minute=1)
    await runner.run(_sampling_params())
    res2 = await runner.run(_sampling_params())
    assert isinstance(res2, _types.ErrorData)
    assert "rate" in res2.message.lower()


@pytest.mark.asyncio
async def test_model_not_in_whitelist_is_rejected():
    p = _FakeProvider(LLMResponse(content="ok", finish_reason="stop"))
    runner = _runner(p, allowed_models=["only-this"])
    params = _sampling_params()
    params.modelPreferences = _types.ModelPreferences(
        hints=[_types.ModelHint(name="forbidden")]
    )
    res = await runner.run(params)
    assert isinstance(res, _types.ErrorData)
    assert "model" in res.message.lower()


@pytest.mark.asyncio
async def test_tool_round_bound_forces_text_after_cap():
    p = _FakeProvider(LLMResponse(
        content=None, finish_reason="tool_calls",
        tool_calls=[ToolCallRequest(id="c", name="loop", arguments={})],
    ))
    runner = _runner(p, max_tool_rounds=1)
    tools = [_types.Tool(
        name="loop", description="d", inputSchema={"type": "object", "properties": {}}
    )]
    r1 = await runner.run(_sampling_params(tools=tools))
    assert isinstance(r1, _types.CreateMessageResultWithTools)
    r2 = await runner.run(_sampling_params(tools=tools))
    # 2nd round exceeds the bound → forced plain text
    assert isinstance(r2, _types.CreateMessageResult)


@pytest.mark.asyncio
async def test_provider_error_becomes_error_data():
    p = _FakeProvider(LLMResponse(content="Error calling LLM: boom", finish_reason="error"))
    runner = _runner(p)
    res = await runner.run(_sampling_params())
    assert isinstance(res, _types.ErrorData)


# ---------------------------------------------------------------------------
# 6d — sampling callback on MCPServerConnection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sampling_callback_delegates_to_runner():
    class _FakeRunner:
        governance = SamplingGovernance(allow_tools=True)

        async def run(self, params):
            return _types.CreateMessageResult(
                role="assistant",
                content=_types.TextContent(type="text", text="from-runner"),
                model="m",
                stopReason="endTurn",
            )

    conn = MCPServerConnection(
        "s", MCPServerConfig(), ToolRegistry(), sampling_runner=_FakeRunner()
    )
    cb = conn._make_sampling_callback()
    res = await cb(context=None, params=_sampling_params())
    assert res.content.text == "from-runner"


def test_session_kwargs_no_sampling_callback_when_no_runner():
    conn = _conn()  # no sampling_runner
    kwargs = conn._session_kwargs()
    assert "sampling_callback" not in kwargs


def test_session_kwargs_includes_sampling_callback_when_runner_set():
    gov = SamplingGovernance(allow_tools=True)
    runner = SamplingRunner(
        provider=object(), default_model="m", governance=gov
    )
    conn = MCPServerConnection(
        "s", MCPServerConfig(), ToolRegistry(), sampling_runner=runner
    )
    kwargs = conn._session_kwargs()
    assert "sampling_callback" in kwargs
    assert callable(kwargs["sampling_callback"])


# ---------------------------------------------------------------------------
# 6d — connect_mcp_servers builds runner when enabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_builds_sampling_runner_when_enabled(monkeypatch):
    """_build_sampling_runner builds a SamplingRunner when sampling is enabled."""
    from durin.agent.tools import mcp as mcp_mod
    from durin.agent.tools.mcp_sampling import SamplingRunner
    from durin.config.schema import MCPSamplingConfig

    class _FakeProvider:
        pass

    cfg = MCPServerConfig(command="x", sampling=MCPSamplingConfig(enabled=True))
    runner = mcp_mod._build_sampling_runner(cfg, _FakeProvider(), "m-default")
    assert isinstance(runner, SamplingRunner)
    assert runner.default_model == "m-default"


@pytest.mark.asyncio
async def test_connect_no_runner_when_sampling_disabled():
    from durin.agent.tools import mcp as mcp_mod

    cfg = MCPServerConfig(command="x")  # sampling.enabled defaults to False
    runner = mcp_mod._build_sampling_runner(cfg, object(), "m")
    assert runner is None


@pytest.mark.asyncio
async def test_connect_no_runner_when_no_provider():
    from durin.agent.tools import mcp as mcp_mod
    from durin.config.schema import MCPSamplingConfig

    cfg = MCPServerConfig(command="x", sampling=MCPSamplingConfig(enabled=True))
    runner = mcp_mod._build_sampling_runner(cfg, None, "m-default")
    assert runner is None
