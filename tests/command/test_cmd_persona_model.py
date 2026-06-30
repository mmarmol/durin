import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.command.router import CommandContext
from durin.config.schema import Config, ModelPresetConfig, PersonaConfig
from durin.providers.factory import ProviderSnapshot
from durin.session.manager import Session


def _ctx(args, model=None):
    cfg = Config(
        personas={
            "tutor": PersonaConfig(soul="default", model=model),
            "plain": PersonaConfig(soul="default", model=None),
        }
    )
    loop = SimpleNamespace(app_config=cfg, model_presets={})
    msg = SimpleNamespace(channel="cli", chat_id="direct", metadata={})
    session = Session(key="cli:direct")
    return CommandContext(
        msg=msg,
        session=session,
        key="cli:direct",
        raw=f"/persona {args}".strip(),
        args=args,
        loop=loop,
    )


def test_switch_to_model_pinned_persona_applies_model():
    ctx = _ctx("tutor", model="anthropic claude-3-5-sonnet-20241022")
    calls = []
    ctx.loop.set_model_preset = lambda *a, **k: calls.append((a, k))
    from durin.command.builtin import cmd_persona

    asyncio.run(cmd_persona(ctx))
    assert calls, "expected set_model_preset to be called for a model-pinned persona"
    # A raw `provider model` pair is registered as an ad-hoc preset first, then
    # applied by its model-name key — never passed raw (which would KeyError).
    assert calls[0] == (("claude-3-5-sonnet-20241022",), {"publish_update": True})
    assert "claude-3-5-sonnet-20241022" in ctx.loop.model_presets


def test_switch_to_persona_without_model_does_not_apply_model():
    ctx = _ctx("plain", model=None)
    calls = []
    ctx.loop.set_model_preset = lambda *a, **k: calls.append((a, k))
    from durin.command.builtin import cmd_persona

    asyncio.run(cmd_persona(ctx))
    assert not calls, "expected set_model_preset NOT to be called for a persona with no model"


def _provider(default_model: str) -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = default_model
    provider.generation = SimpleNamespace(max_tokens=4096, temperature=0.1, reasoning_effort=None)
    return provider


def _real_loop(tmp_path, persona_model: str) -> AgentLoop:
    """A genuine AgentLoop (NOT a stub for ``set_model_preset``) wired with a
    persona pinned to a raw ``provider model`` pair that is not a registered
    preset, so ``cmd_persona`` drives the real ``normalize_preset_name`` path."""

    def loader(name, preset=None):
        target = preset or ModelPresetConfig(model=name)
        return ProviderSnapshot(
            provider=_provider(target.model),
            model=target.model,
            context_window_tokens=target.context_window_tokens,
            signature=("model_preset", name, target.model),
        )

    cfg = Config(personas={"tutor": PersonaConfig(soul="default", model=persona_model)})
    return AgentLoop(
        bus=MessageBus(),
        provider=_provider("base-model"),
        workspace=tmp_path,
        model="base-model",
        context_window_tokens=1000,
        model_presets={"default": ModelPresetConfig(model="base-model")},
        preset_snapshot_loader=loader,
        app_config=cfg,
    )


def test_raw_provider_model_pair_persona_applies_without_keyerror(tmp_path):
    """Regression: a persona pinned to a raw ``provider model`` pair (the webui
    persona editor's catalog-pick form) must apply through the same ad-hoc
    preset path ``/model`` uses — not raise a KeyError out of the command."""
    pair = "anthropic claude-3-5-sonnet-20241022"
    loop = _real_loop(tmp_path, pair)
    msg = SimpleNamespace(channel="cli", chat_id="direct", metadata={})
    session = Session(key="cli:direct")
    ctx = CommandContext(
        msg=msg,
        session=session,
        key="cli:direct",
        raw="/persona tutor",
        args="tutor",
        loop=loop,
    )

    from durin.command.builtin import cmd_persona

    out = asyncio.run(cmd_persona(ctx))  # must not raise

    assert "Switched persona to `tutor`" in out.content
    # The ad-hoc preset is registered and the active model reflects the pair.
    assert "claude-3-5-sonnet-20241022" in loop.model_presets
    assert loop.model == "claude-3-5-sonnet-20241022"
    assert session.metadata.get("persona") == "tutor"
