"""A preset picked at runtime (/model) is held until the configuration
selects anew, whatever else changes under a running gateway.

The configuration's selection is the preset it names in
agents.defaults.model_preset (the implicit default when it names none) and
that preset's own settings. Only another preset, or an edit to the selected
preset's own settings, is a new selection, and the loop then follows the
configuration. A refresh of the model catalog, a model-capability override,
a credential, the fallback chain, agents.defaults' compaction numbers or an
unrelated default, or an edit to any other preset (the picked one included)
leaves the pick in place.

Each seed plays a random sequence of /model picks and configuration changes
against a loop wired as the gateway wires it (its provider snapshot and
default-preset loaders read the configuration of the moment), refreshing
before each turn as the gateway does, and checks after every refresh which
preset the loop holds and which model it runs. ``DURIN_INVARIANT_SEEDS``
runs a tenth as many sequences as the turn harness runs scenarios, never
fewer than the default."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

import durin.providers.provider_catalog as provider_catalog
from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.config.schema import Config, ModelCapabilityOverride, ModelEntry, ModelPresetConfig
from durin.providers.factory import build_provider_snapshot

_DEFAULT_SEQUENCES = 10
_SEQUENCES = max(_DEFAULT_SEQUENCES, int(os.environ.get("DURIN_INVARIANT_SEEDS") or 0) // 10)
_STEPS = 30

# Two presets share the default's model and provider, so a change between
# them is told apart by the preset alone.
_PRESETS = {
    "fast": {"model": "gpt-4.1-mini"},
    "big": {"model": "gpt-4.1", "temperature": 0.7},
    "twin": {"model": "gpt-4.1", "temperature": 0.2},
    "wide": {"model": "gpt-4o", "preemptive_compact_max_tokens": 100_000},
}
_MODELS = ("gpt-4.1", "gpt-4.1-mini", "gpt-4o")


@dataclass
class _World:
    """The configuration a running gateway reads, and the catalog rows."""

    config: Config
    catalog: dict[str, tuple[int, int]] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)

    def selected(self) -> str | None:
        name = self.config.agents.defaults.model_preset
        return None if name in (None, "default") else name

    def selected_model(self) -> str:
        name = self.selected()
        return self.config.agents.defaults.model if name is None else self.config.model_presets[name].model


def _config(workspace: str) -> Config:
    config = Config()
    config.agents.defaults.workspace = workspace
    config.agents.defaults.model = "gpt-4.1"
    config.agents.defaults.provider = "openai"
    config.providers.openai.api_key = "sk-test-not-a-key"
    config.memory.file_watcher.enabled = False
    config.memory.health_check.enabled = False
    config.catalog_refresh.enabled = False
    config.mcp_catalog_refresh.enabled = False
    for name, fields in _PRESETS.items():
        config.model_presets[name] = ModelPresetConfig(provider="openai", **fields)
    return config


def _loop(world: _World) -> AgentLoop:
    """The loop as the gateway builds it: from the configuration, with the
    snapshot of what it selects, and loaders that read it anew."""

    def load_provider_snapshot(config_path=None, *, preset_name=None, preset=None):
        return build_provider_snapshot(world.config, preset_name=preset_name, preset=preset)

    first = load_provider_snapshot()
    return AgentLoop.from_config(
        world.config, MessageBus(),
        provider=first.provider, model=first.model, context_window_tokens=first.context_window_tokens,
        provider_snapshot_loader=load_provider_snapshot,
        default_preset_loader=lambda: world.config.resolve_default_preset(),
        provider_signature=first.signature,
        provider_selection=first.selection,
    )


def _change(world: _World, rng: random.Random) -> bool:
    """Apply one configuration change; True when it is a new selection."""
    config = world.config.model_copy(deep=True)
    defaults = config.agents.defaults
    selected = world.selected()
    others = [name for name in _PRESETS if name != selected]
    kind = rng.choice((
        "catalog", "capability", "credential", "fallback", "other_preset", "defaults_compaction",
        "unrelated_default", "select", "edit_selected",
    ))
    replaces = False
    if kind == "catalog":
        model = rng.choice(_MODELS)
        world.catalog[model] = (rng.choice((128_000, 400_000, 1_000_000, 1_047_576)), rng.choice((16_384, 32_768)))
        detail = f"{model} {world.catalog[model]}"
    elif kind == "capability":
        model = rng.choice(_MODELS)
        config.model_capabilities[model] = ModelCapabilityOverride(
            max_input_tokens=rng.choice((200_000, 500_000)), supports_vision=rng.random() < 0.5,
        )
        detail = model
    elif kind == "credential":
        config.providers.openai.api_key = f"sk-test-{rng.randrange(10**6)}"
        if rng.random() < 0.5:
            config.providers.openai.api_base = f"https://proxy{rng.randrange(9)}.example/v1"
        detail = "openai key/base"
    elif kind == "fallback":
        defaults.fallback_models = rng.sample(sorted(_PRESETS), rng.randint(0, 2))
        detail = str(defaults.fallback_models)
    elif kind == "other_preset":
        name = rng.choice(others)
        config.model_presets[name] = config.model_presets[name].model_copy(update={
            "temperature": round(rng.uniform(0, 1), 2),
            "preemptive_compact_max_tokens": rng.choice((None, 0, 64_000, 300_000)),
        })
        detail = name
    elif kind == "defaults_compaction":
        defaults.preemptive_compact_ratio = rng.choice((0.3, 0.5, 0.7))
        defaults.preemptive_compact_max_tokens = rng.choice((None, 64_000, 128_000, 256_000))
        detail = f"{defaults.preemptive_compact_ratio}/{defaults.preemptive_compact_max_tokens}"
    elif kind == "unrelated_default":
        defaults.max_tool_iterations = rng.randint(20, 80)
        defaults.timezone = rng.choice(("UTC", "Europe/Madrid", "America/Bogota"))
        detail = "max_tool_iterations/timezone"
    elif kind == "select":
        choices = [n for n in (None, *_PRESETS) if n != selected]
        defaults.model_preset = rng.choice(choices)
        detail = str(defaults.model_preset)
        replaces = True
    else:
        # A value the selected preset does not have yet, so it changes.
        if selected is None:
            # The implicit default's own settings: agents.defaults' or its
            # model's entry under the provider.
            entry = config.providers.openai.models.get(defaults.model)
            if rng.random() < 0.5:
                defaults.temperature = rng.choice([t for t in (0.1, 0.3, 0.6, 0.9) if t != defaults.temperature])
                detail = f"agents.defaults.temperature={defaults.temperature}"
            else:
                current = entry.max_tokens if entry and entry.max_tokens is not None else defaults.max_tokens
                max_tokens = rng.choice([m for m in (8_192, 12_000, 16_384) if m != current])
                config.providers.openai.models[defaults.model] = ModelEntry(max_tokens=max_tokens)
                detail = f"providers.openai.models.{defaults.model}.max_tokens={max_tokens}"
        else:
            preset = config.model_presets[selected]
            temperature = rng.choice([t for t in (0.1, 0.3, 0.6, 0.9) if t != preset.temperature])
            config.model_presets[selected] = preset.model_copy(update={"temperature": temperature})
            detail = f"model_presets.{selected}.temperature={temperature}"
        replaces = True
    world.config = config
    world.log.append(f"{kind}: {detail}")
    return replaces


@pytest.mark.parametrize("seed", range(_SEQUENCES))
def test_a_runtime_pick_is_held_until_the_configuration_selects_anew(tmp_path, monkeypatch, seed):
    rng = random.Random(seed)
    world = _World(config=_config(str(tmp_path)))
    real_limits = provider_catalog.catalog_model_limits

    def catalog_model_limits(provider: str, model: str):
        # The daily models.dev refresh rewrites these rows under a running
        # gateway without the configuration changing.
        if model in world.catalog:
            window, output = world.catalog[model]
            return SimpleNamespace(max_input_tokens=window, max_output_tokens=output)
        return real_limits(provider, model)

    monkeypatch.setattr(provider_catalog, "catalog_model_limits", catalog_model_limits)
    loop = _loop(world)
    held = world.selected()
    for step in range(_STEPS):
        if rng.random() < 0.3:
            pick = rng.choice(sorted(_PRESETS))
            loop.set_model_preset(pick)
            held = pick
            world.log.append(f"/model {pick}")
        elif _change(world, rng):
            held = None
        # What the gateway does at the start of every turn.
        loop._refresh_provider_snapshot()

        expected_model = world.config.model_presets[held].model if held else world.selected_model()
        context = f"seed {seed}, step {step}: " + " | ".join(world.log[-6:])
        assert loop.model_preset == held, context
        assert loop.model == expected_model, context
