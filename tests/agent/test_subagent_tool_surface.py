"""The background (subagent-scope) tool surface: what subagents and workflow
work nodes can and cannot reach, and how the aux bridges register there."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from durin.agent.subagent import SubagentManager
from durin.agent.tools.context import AuxProviderHandle, ToolContext
from durin.agent.tools.loader import ToolLoader
from durin.agent.tools.registry import ToolRegistry
from durin.bus.queue import MessageBus
from durin.config.schema import ToolsConfig
from durin.providers.base import LLMProvider

# Tools that must be reachable from a background worker (with aux configured).
BACKGROUND_EXPECTED = {
    "read_file",
    "write_file",
    "exec",
    "convert_to_markdown",
    "interpret_image",
    "interpret_audio",
    "memory_upsert_entity",
    "memory_ingest",
    "memory_read_entity",
    "memory_entity_lineage",
    "memory_source_session",
}

# Interactive / orchestration / destructive tools that must never load in
# subagent scope, regardless of how rich the context is.
BACKGROUND_FORBIDDEN = {
    "spawn",
    "subagent_monitor",
    "subagent_output",
    "run_workflow",
    "ask_user_question",
    "message",
    "cron",
    "memory_forget",
    "skill_edit",
    "skill_import",
    "enter_plan_mode",
    "exit_plan_mode",
}


def _rich_subagent_ctx(workspace: Path, *, aux: bool) -> ToolContext:
    handles = {}
    if aux:
        handles = {
            "vision": AuxProviderHandle(provider=MagicMock(), model="vis-model"),
            "audio": AuxProviderHandle(provider=MagicMock(), model="aud-model"),
        }
    return ToolContext(
        config=ToolsConfig(),
        workspace=str(workspace),
        scope="subagent",
        aux_providers=handles,
    )


def test_subagent_scope_surface(tmp_path):
    registry = ToolRegistry()
    ToolLoader().load(_rich_subagent_ctx(tmp_path, aux=True), registry, scope="subagent")
    names = set(registry.tool_names)
    missing = BACKGROUND_EXPECTED - names
    assert not missing, f"expected in subagent scope but absent: {missing}"
    leaked = BACKGROUND_FORBIDDEN & names
    assert not leaked, f"must never load in subagent scope: {leaked}"


def test_bridges_hidden_without_aux_models(tmp_path):
    registry = ToolRegistry()
    ToolLoader().load(_rich_subagent_ctx(tmp_path, aux=False), registry, scope="subagent")
    assert not registry.has("interpret_image")
    assert not registry.has("interpret_audio")
    assert registry.has("convert_to_markdown")


def _manager(tmp_path, app_config_getter=None) -> SubagentManager:
    provider = MagicMock(spec=LLMProvider)
    provider.get_default_model.return_value = "test"
    return SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=MessageBus(),
        model="test",
        max_tool_result_chars=16_000,
        app_config_getter=app_config_getter,
    )


def test_subagent_manager_registers_bridges(tmp_path, monkeypatch):
    import durin.agent.aux_bridges as aux_bridges

    app_config = object()
    monkeypatch.setattr(
        aux_bridges,
        "build_aux_providers",
        lambda cfg: {"vision": AuxProviderHandle(provider=MagicMock(), model="vis")},
    )
    tools = _manager(tmp_path, app_config_getter=lambda: app_config)._build_tools()
    assert tools.has("interpret_image")
    assert not tools.has("interpret_audio")
    assert not tools.has("spawn")


def test_subagent_manager_survives_aux_failure(tmp_path, monkeypatch):
    import durin.agent.aux_bridges as aux_bridges

    def _boom(cfg):
        raise RuntimeError("provider construction failed")

    monkeypatch.setattr(aux_bridges, "build_aux_providers", _boom)
    tools = _manager(tmp_path, app_config_getter=lambda: object())._build_tools()
    assert tools.has("read_file")
    assert not tools.has("interpret_image")


def _node_runner(tmp_path, app_config):
    from durin.workflow.node_runner import AgentNodeRunner

    return AgentNodeRunner(
        runner=MagicMock(),
        sessions=SimpleNamespace(workspace=tmp_path),
        default_model="test",
        app_config=app_config,
    )


def test_node_default_tools_include_bridges(tmp_path, monkeypatch):
    import durin.agent.aux_bridges as aux_bridges

    calls = []

    def _fake_build(cfg):
        calls.append(cfg)
        return {"vision": AuxProviderHandle(provider=MagicMock(), model="vis")}

    monkeypatch.setattr(aux_bridges, "build_aux_providers", _fake_build)
    nr = _node_runner(tmp_path, app_config=object())
    node = SimpleNamespace(tools="default", mcps=(), mode="build")

    registry = nr._build_tools(node)
    assert registry.has("interpret_image")
    assert registry.has("memory_upsert_entity")
    assert not registry.has("spawn")
    assert not registry.has("run_workflow")

    # The aux handles are built once per runner, shared by every node.
    nr._build_tools(node)
    assert len(calls) == 1


def test_node_tools_none_stays_empty(tmp_path, monkeypatch):
    import durin.agent.aux_bridges as aux_bridges

    monkeypatch.setattr(
        aux_bridges,
        "build_aux_providers",
        lambda cfg: pytest.fail("aux bridges must not be built for a toolless node"),
    )
    nr = _node_runner(tmp_path, app_config=object())
    node = SimpleNamespace(tools="none", mcps=(), mode="build")
    assert nr._build_tools(node).tool_names == []


def test_read_mode_subtracts_writes_but_keeps_bridges(tmp_path, monkeypatch):
    import durin.agent.aux_bridges as aux_bridges

    monkeypatch.setattr(
        aux_bridges,
        "build_aux_providers",
        lambda cfg: {"vision": AuxProviderHandle(provider=MagicMock(), model="vis")},
    )
    nr = _node_runner(tmp_path, app_config=object())
    node = SimpleNamespace(tools="default", mcps=(), mode="read")
    registry = nr._build_tools(node)
    assert registry.has("read_file")
    assert registry.has("interpret_image")
    assert not registry.has("write_file")
    assert not registry.has("memory_upsert_entity")
