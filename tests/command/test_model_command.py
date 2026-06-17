from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from durin.agent.loop import AgentLoop
from durin.bus.events import InboundMessage
from durin.bus.queue import MessageBus
from durin.command.builtin import (
    build_help_text,
    builtin_command_palette,
    cmd_goal,
    cmd_model,
    register_builtin_commands,
)
from durin.command.router import CommandContext, CommandRouter
from durin.config.schema import ModelPresetConfig
from durin.providers.factory import ProviderSnapshot


def _provider(default_model: str, max_tokens: int = 123) -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = default_model
    provider.generation = SimpleNamespace(
        max_tokens=max_tokens,
        temperature=0.1,
        reasoning_effort=None,
    )
    return provider


def _make_loop(tmp_path) -> AgentLoop:
    return AgentLoop(
        bus=MessageBus(),
        provider=_provider("base-model", max_tokens=123),
        workspace=tmp_path,
        model="base-model",
        context_window_tokens=1000,
        model_presets={
            "default": ModelPresetConfig(
                model="base-model",
                max_tokens=123,
                context_window_tokens=1000,
            ),
            "fast": ModelPresetConfig(
                model="openai/gpt-4.1",
                max_tokens=4096,
                context_window_tokens=32_768,
            ),
        },
    )


def _ctx(loop: AgentLoop, raw: str, args: str = "") -> CommandContext:
    msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content=raw)
    return CommandContext(msg=msg, session=None, key=msg.session_key, raw=raw, args=args, loop=loop)


def _ctx_session(loop: AgentLoop, raw: str, args: str = "") -> CommandContext:
    msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content=raw)
    return CommandContext(
        msg=msg, session=MagicMock(), key=msg.session_key, raw=raw, args=args, loop=loop,
    )


@pytest.mark.asyncio
async def test_model_command_lists_current_and_available_presets(tmp_path) -> None:
    loop = _make_loop(tmp_path)

    out = await cmd_model(_ctx(loop, "/model"))

    assert "Current model: `base-model`" in out.content
    assert "Current preset: `default`" in out.content
    assert "Available presets: `default`, `fast`" in out.content
    assert "`fast`" in out.content
    assert out.metadata == {"render_as": "text"}


@pytest.mark.asyncio
async def test_model_command_switches_preset(tmp_path) -> None:
    loop = _make_loop(tmp_path)

    out = await cmd_model(_ctx(loop, "/model fast", args="fast"))

    assert "Switched model preset to `fast`." in out.content
    assert "Model: `openai/gpt-4.1`" in out.content
    assert loop.model_preset == "fast"
    assert loop.model == "openai/gpt-4.1"
    assert loop.subagents.model == "openai/gpt-4.1"
    assert loop.consolidator.model == "openai/gpt-4.1"


@pytest.mark.asyncio
async def test_model_command_switches_back_to_default(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    loop.set_model_preset("fast")

    out = await cmd_model(_ctx(loop, "/model default", args="default"))

    assert "Switched model preset to `default`." in out.content
    assert loop.model_preset == "default"
    assert loop.model == "base-model"
    assert loop.context_window_tokens == 1000


@pytest.mark.asyncio
async def test_model_command_arbitrary_name_creates_temp_preset(tmp_path) -> None:
    loop = _make_loop(tmp_path)

    out = await cmd_model(_ctx(loop, "/model glm-5.2", args="glm-5.2"))

    assert "Switched model preset" in out.content
    assert "glm-5.2" in out.content
    assert loop.model == "glm-5.2"
    assert "glm-5.2" in loop.model_presets


@pytest.mark.asyncio
async def test_model_command_arbitrary_name_switches_under_config_loader(
    tmp_path,
) -> None:
    """End-to-end mirror of the webui/TUI bug: ``/model <arbitrary>`` must
    switch even when the loop resolves presets through a loader that re-reads
    the on-disk config (the gateway's ``load_provider_snapshot``), which never
    saw the runtime injection. Previously this returned 'Could not switch model
    preset: ... not found in model_presets' while listing the name as available.
    """
    new_provider = _provider("glm-5v-turbo", max_tokens=4096)
    on_disk = {"default": ModelPresetConfig(model="base-model")}

    def loader(name, preset=None):
        target = preset if preset is not None else on_disk.get(name)
        if target is None:
            raise KeyError(f"model_preset {name!r} not found in model_presets")
        return ProviderSnapshot(
            provider=new_provider,
            model=target.model,
            context_window_tokens=target.context_window_tokens,
            signature=("model_preset", name, target.model),
        )

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_provider("base-model", max_tokens=123),
        workspace=tmp_path,
        model="base-model",
        context_window_tokens=1000,
        model_presets={"default": ModelPresetConfig(model="base-model")},
        preset_snapshot_loader=loader,
    )

    out = await cmd_model(_ctx(loop, "/model glm-5v-turbo", args="glm-5v-turbo"))

    assert "Could not switch" not in out.content
    assert "Switched model preset to `glm-5v-turbo`." in out.content
    assert loop.model == "glm-5v-turbo"
    assert "glm-5v-turbo" in loop.model_presets


@pytest.mark.asyncio
async def test_model_command_provider_model_pair_uses_explicit_provider(tmp_path) -> None:
    captured = {}
    new_provider = _provider("glm-5v-turbo", max_tokens=4096)

    def loader(name, preset=None):
        captured["preset"] = preset
        target = preset or ModelPresetConfig(model=name)
        return ProviderSnapshot(
            provider=new_provider, model=target.model,
            context_window_tokens=target.context_window_tokens,
            signature=("model_preset", name, target.model),
        )

    loop = AgentLoop(
        bus=MessageBus(), provider=_provider("base-model"),
        workspace=tmp_path, model="base-model", context_window_tokens=1000,
        model_presets={"default": ModelPresetConfig(model="base-model")},
        preset_snapshot_loader=loader,
    )
    out = await cmd_model(_ctx(loop, "/model zai_coding_plan glm-5v-turbo",
                               args="zai_coding_plan glm-5v-turbo"))
    assert "Could not switch" not in out.content
    assert loop.model == "glm-5v-turbo"
    assert captured["preset"].provider == "zai_coding_plan"


@pytest.mark.asyncio
async def test_model_command_bare_name_uses_active_provider(tmp_path) -> None:
    from durin.config.schema import Config

    captured = {}

    def loader(name, preset=None):
        captured["preset"] = preset
        target = preset or ModelPresetConfig(model=name)
        return ProviderSnapshot(
            provider=_provider(target.model), model=target.model,
            context_window_tokens=target.context_window_tokens,
            signature=("model_preset", name, target.model),
        )

    app_config = Config()
    app_config.agents.defaults.provider = "openai_codex"
    loop = AgentLoop(
        bus=MessageBus(), provider=_provider("base-model"),
        workspace=tmp_path, model="base-model", context_window_tokens=1000,
        model_presets={"default": ModelPresetConfig(model="base-model")},
        preset_snapshot_loader=loader, app_config=app_config,
    )
    out = await cmd_model(_ctx(loop, "/model some-new-model", args="some-new-model"))
    assert "Could not switch" not in out.content
    assert captured["preset"].provider == "openai_codex"


def test_arbitrary_model_preset_applies_catalog_capabilities() -> None:
    """A temp preset for a known catalog model carries its real capabilities,
    not the schema defaults (65536 / 8192). Regression: glm-5.2 switched with a
    65536 context window instead of its real 1M.
    """
    from durin.command.builtin import _arbitrary_model_preset

    p = _arbitrary_model_preset("glm-5.2", "zai_coding_plan")
    assert p.provider == "zai_coding_plan"
    assert p.context_window_tokens == 1_000_000
    assert p.max_tokens == 131072


@pytest.mark.asyncio
async def test_model_command_applies_caps_and_shows_provider(tmp_path) -> None:
    new_provider = _provider("glm-5.2", max_tokens=131072)

    def loader(name, preset=None):
        target = preset or ModelPresetConfig(model=name)
        new_provider.generation = SimpleNamespace(
            max_tokens=target.max_tokens, temperature=0.1, reasoning_effort=None,
        )
        return ProviderSnapshot(
            provider=new_provider, model=target.model,
            context_window_tokens=target.context_window_tokens,
            signature=("model_preset", name, target.model),
        )

    loop = AgentLoop(
        bus=MessageBus(), provider=_provider("base-model"),
        workspace=tmp_path, model="base-model", context_window_tokens=1000,
        model_presets={"default": ModelPresetConfig(model="base-model")},
        preset_snapshot_loader=loader,
    )
    out = await cmd_model(
        _ctx(loop, "/model zai_coding_plan glm-5.2", args="zai_coding_plan glm-5.2")
    )
    assert "Could not switch" not in out.content
    assert "- Provider: `zai_coding_plan`" in out.content
    assert "Context window: 1000000" in out.content
    assert "Max output tokens: 131072" in out.content


@pytest.mark.asyncio
async def test_model_command_does_not_depend_on_my_allow_set(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    assert loop.tools_config.my.allow_set is False

    await cmd_model(_ctx(loop, "/model fast", args="fast"))

    assert loop.model_preset == "fast"


@pytest.mark.asyncio
async def test_model_command_registered_as_exact_and_prefix(tmp_path) -> None:
    router = CommandRouter()
    register_builtin_commands(router)
    loop = _make_loop(tmp_path)

    out = await router.dispatch(_ctx(loop, "/model fast"))

    assert out is not None
    assert "Switched model preset" in out.content
    assert loop.model_preset == "fast"


def test_model_command_in_help_and_palette() -> None:
    palette = builtin_command_palette()

    assert any(item["command"] == "/model" and item["arg_hint"] == "[preset]" for item in palette)
    assert "/model [preset]" in build_help_text()


@pytest.mark.asyncio
async def test_goal_command_shows_usage_without_args(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_goal(_ctx(loop, "/goal"))
    assert out is not None
    assert "Usage: /goal" in out.content


@pytest.mark.asyncio
async def test_goal_command_rejects_mid_turn_without_session(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    out = await cmd_goal(_ctx(loop, "/goal do work", args="do work"))
    assert out is not None
    assert "/stop" in out.content


@pytest.mark.asyncio
async def test_goal_command_rewrites_to_agent_prompt(tmp_path) -> None:
    loop = _make_loop(tmp_path)
    ctx = _ctx_session(loop, "/goal audit the repo", args="audit the repo")
    out = await cmd_goal(ctx)
    assert out is None
    assert "audit the repo" in ctx.msg.content
    assert "long_task" in ctx.msg.content
    assert ctx.msg.metadata.get("original_command") == "/goal"
    assert ctx.msg.metadata.get("original_content") == "/goal audit the repo"
    assert isinstance(ctx.msg.metadata.get("goal_started_at"), int | float)


@pytest.mark.asyncio
async def test_goal_command_registered_on_router(tmp_path) -> None:
    router = CommandRouter()
    register_builtin_commands(router)
    loop = _make_loop(tmp_path)
    ctx = _ctx_session(loop, "/goal ship it", args="ship it")
    out = await router.dispatch(ctx)
    assert out is None
    assert "ship it" in ctx.msg.content


def test_goal_command_in_help_and_palette() -> None:
    palette = builtin_command_palette()
    assert any(item["command"] == "/goal" and item["arg_hint"] == "<goal>" for item in palette)
    assert "/goal <goal>" in build_help_text()
