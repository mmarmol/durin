from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from durin.agent.loop import AgentLoop
from durin.bus.queue import MessageBus
from durin.providers.factory import ProviderSnapshot


def _provider(default_model: str, max_tokens: int = 123) -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = default_model
    provider.generation = SimpleNamespace(max_tokens=max_tokens)
    return provider


def test_provider_refresh_updates_all_model_dependents(tmp_path: Path) -> None:
    old_provider = _provider("old-model")
    new_provider = _provider("new-model", max_tokens=456)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=old_provider,
        workspace=tmp_path,
        model="old-model",
        context_window_tokens=1000,
        provider_snapshot_loader=lambda: ProviderSnapshot(
            provider=new_provider,
            model="new-model",
            context_window_tokens=2000,
            signature=("new-model",),
        ),
    )

    loop._refresh_provider_snapshot()

    assert loop.provider is new_provider
    assert loop.model == "new-model"
    assert loop.context_window_tokens == 2000
    assert loop.runner.provider is new_provider
    assert loop.subagents.provider is new_provider
    assert loop.subagents.model == "new-model"
    assert loop.subagents.runner.provider is new_provider
    assert loop.consolidator.provider is new_provider
    assert loop.consolidator.model == "new-model"
    assert loop.consolidator.context_window_tokens == 2000
    assert loop.consolidator.max_completion_tokens == 456


# A gateway's refresh, through its own loaders on a real config file. Every
# preset here runs the same model on the same provider: they differ only in
# their compaction cap, which the consolidator's trigger reports.


def _config(tmp_path: Path, active: str | None, caps: dict[str, int]):
    from durin.config.schema import Config, ModelPresetConfig

    config = Config()
    config.agents.defaults.model = "gpt-4.1"
    config.agents.defaults.provider = "openai"
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    config.agents.defaults.model_preset = active
    config.providers.openai.api_key = "sk-test-not-real"
    for name, cap in caps.items():
        config.model_presets[name] = ModelPresetConfig(
            model="gpt-4.1", provider="openai", context_window_tokens=1_000_000,
            preemptive_compact_max_tokens=cap,
        )
    return config


def _gateway(tmp_path: Path, config, *, startup_selection: bool = False) -> tuple[AgentLoop, Path]:
    """A loop wired as the gateway wires it, from the file at *path*;
    *startup_selection* also hands it the selection of the snapshot it
    started from, as the gateway does."""
    from durin.config.loader import load_config, save_config
    from durin.providers.factory import load_default_preset, load_provider_snapshot

    path = tmp_path / "config.json"
    save_config(config, path)

    def loader(*_args, **kwargs):
        return load_provider_snapshot(path, **kwargs)

    first = loader()
    extra = {"provider_selection": first.selection} if startup_selection else {}
    loop = AgentLoop.from_config(
        load_config(path), MessageBus(), provider=first.provider, model=first.model,
        context_window_tokens=first.context_window_tokens,
        provider_snapshot_loader=loader, default_preset_loader=lambda: load_default_preset(path),
        **extra,
    )
    return loop, path


def _cap(loop: AgentLoop) -> int:
    loop._refresh_provider_snapshot()
    return loop.consolidator._preemptive_trigger()[0]


def test_a_switch_to_a_preset_on_the_same_model_applies_from_the_next_turn(tmp_path: Path) -> None:
    """agents.defaults.model_preset changed while the gateway runs, to a
    preset on the same model and provider with other settings: the refresh
    told selections apart by model and provider alone, and kept the old
    preset until a restart."""
    from durin.config.loader import save_config

    config = _config(tmp_path, "fast", {"fast": 300_000, "twin": 450_000})
    loop, path = _gateway(tmp_path, config)
    assert _cap(loop) == 300_000

    config.agents.defaults.model_preset = "twin"
    save_config(config, path)
    assert _cap(loop) == 450_000


def test_an_edit_to_the_selected_preset_applies_from_the_next_turn(tmp_path: Path) -> None:
    """An edit to the preset agents.defaults.model_preset names changes what
    the file selects as much as a switch does."""
    from durin.config.loader import save_config

    config = _config(tmp_path, "fast", {"fast": 300_000})
    loop, path = _gateway(tmp_path, config)
    assert _cap(loop) == 300_000

    config.model_presets["fast"].preemptive_compact_max_tokens = 450_000
    save_config(config, path)
    assert _cap(loop) == 450_000


def test_the_preset_the_gateway_started_with_stays_active_until_the_file_changes(tmp_path: Path) -> None:
    config = _config(tmp_path, "fast", {"fast": 300_000})
    loop, _path = _gateway(tmp_path, config, startup_selection=True)

    assert _cap(loop) == 300_000
    assert loop.model_preset == "fast"


def test_a_runtime_pick_survives_its_own_reload_and_edits_elsewhere(tmp_path: Path) -> None:
    """A preset picked at runtime (/model) is held until the file selects
    anew. Neither an edit to agents.defaults' cap nor a reload of the pick's
    own settings is a new selection; after the reload, the refresh recorded
    the pick's model as the file's selection and dropped the pick a turn
    later."""
    from durin.config.loader import save_config

    config = _config(tmp_path, None, {"big": 400_000})
    config.model_presets["big"].model = "gpt-4.1-mini"
    loop, path = _gateway(tmp_path, config)
    loop.set_model_preset("big", publish_update=False)
    assert _cap(loop) == 400_000

    config.agents.defaults.preemptive_compact_max_tokens = 300_000
    save_config(config, path)
    assert _cap(loop) == 400_000
    assert loop.model_preset == "big"

    config.model_presets["big"].preemptive_compact_max_tokens = 500_000
    save_config(config, path)
    loop.model_presets["big"] = config.model_presets["big"].model_copy()
    assert _cap(loop) == 500_000
    assert _cap(loop) == 500_000
    assert loop.model_preset == "big"


def test_a_runtime_pick_survives_a_catalog_change_to_the_default_models_limits(
    tmp_path: Path, monkeypatch,
) -> None:
    """The catalog the model limits come from is refreshed daily. The
    selection the refresh compares held the default preset with the window
    and output limit resolved from it, so a new catalog row for the default
    model read as a new choice in the file and dropped a /model pick."""
    import durin.providers.provider_catalog as catalog

    windows = {"gpt-4.1": 1_047_576}

    def _limits(_provider: str, model: str):
        if model not in windows:
            return None
        return SimpleNamespace(max_input_tokens=windows[model], max_output_tokens=32_768)

    monkeypatch.setattr(catalog, "catalog_model_limits", _limits)
    config = _config(tmp_path, None, {"big": 400_000})
    loop, _path = _gateway(tmp_path, config, startup_selection=True)
    loop.set_model_preset("big", publish_update=False)
    assert _cap(loop) == 400_000

    windows["gpt-4.1"] = 1_000_000
    assert _cap(loop) == 400_000
    assert loop.model_preset == "big"
