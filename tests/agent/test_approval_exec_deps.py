"""The live handles an approval runs with once its turn stopped waiting."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from durin.agent.approval_executors import ExecDeps
from durin.agent.loop import AgentLoop
from durin.agent.tools.shell import ExecToolConfig
from durin.bus.queue import MessageBus
from durin.config.schema import ToolsConfig
from durin.providers.base import GenerationSettings, LLMResponse
from durin.service.mcp import McpService


def _make_loop(tmp_path, *, tools_config: ToolsConfig | None = None) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    response = LLMResponse(content="ok", tool_calls=[])
    provider.chat_with_retry = AsyncMock(return_value=response)
    provider.chat_stream_with_retry = AsyncMock(return_value=response)
    return AgentLoop(
        bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model",
        tools_config=tools_config,
    )


def test_approval_exec_deps_are_the_loops_live_handles(tmp_path):
    loop = _make_loop(tmp_path)
    deps = loop.approval_exec_deps()
    assert isinstance(deps, ExecDeps)
    # The non-asking runner: an approved request must never be able to open
    # a second, nested approval mid-decision.
    assert deps.exec_run == loop.tools.get("exec")._run
    assert isinstance(deps.mcp, McpService)
    # Bound to this loop's live connections, not a config-only view.
    assert deps.mcp._runtime._loop is loop
    # The gateway's out-of-turn deps never carries the literal command (only
    # the turn that filed the request holds that, in ExecTool's own state)
    # or an attribution — an approval decided here always resolves to
    # "failed" rather than running with a guessed identity.
    assert deps.extra == {}
    assert deps.attribution is None


def test_approval_exec_deps_exec_run_is_none_when_exec_is_disabled(tmp_path):
    loop = _make_loop(tmp_path, tools_config=ToolsConfig(exec=ExecToolConfig(enable=False)))
    assert loop.tools.get("exec") is None
    deps = loop.approval_exec_deps()
    assert deps.exec_run is None
