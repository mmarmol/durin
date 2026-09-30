"""Tests for `durin config` get/set/show/edit/path subcommands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from durin.cli.commands import app
from durin.cli.config_cmd import (
    _normalize_dotted_path,
    get_at,
    mask_secrets,
    parse_value,
    path_segments,
    set_at,
    validate_dict,
)
from durin.config.schema import Config, ModelEntry

runner = CliRunner()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_get_at_walks_nested_dicts() -> None:
    data = {"a": {"b": {"c": 42}}}
    assert get_at(data, "a.b.c") == 42


def test_get_at_supports_list_indices() -> None:
    data = {"xs": [10, 20, 30]}
    assert get_at(data, "xs.1") == 20


def test_get_at_raises_for_missing_key() -> None:
    with pytest.raises(KeyError):
        get_at({"a": 1}, "b")


def test_set_at_creates_intermediate_dicts() -> None:
    out = set_at({}, "providers.zhipu.api_key", "sk-X")
    assert out == {"providers": {"zhipu": {"api_key": "sk-X"}}}


def test_set_at_overwrites_existing_scalar() -> None:
    out = set_at({"agents": {"defaults": {"model": "old"}}}, "agents.defaults.model", "new")
    assert out["agents"]["defaults"]["model"] == "new"


def test_set_at_does_not_mutate_input() -> None:
    src = {"a": {"b": 1}}
    set_at(src, "a.b", 2)
    assert src == {"a": {"b": 1}}


@pytest.mark.parametrize("path", ["xs.1.a", "xs[1].a", "xs.1"])
def test_set_at_writes_into_a_list_item(path: str) -> None:
    """A list is addressed by index; it used to be replaced by a dict, so
    validation rejected the whole config ("Input should be a valid list")."""
    src = {"xs": [{"a": 1}, {"a": 2}]}
    out = set_at(src, path, 5)
    expected = {"a": 5} if path.endswith(".a") else 5
    assert out == {"xs": [{"a": 1}, expected]}


@pytest.mark.parametrize("index", ["2", "-1", "x"])
def test_set_at_refuses_an_index_the_list_does_not_have(index: str) -> None:
    with pytest.raises(ValueError, match="xs"):
        set_at({"xs": [{"a": 1}, {"a": 2}]}, f"xs.{index}.a", 5)


def test_set_at_refuses_to_write_inside_a_plain_value() -> None:
    """``model`` holds a string: writing ``model.foo`` used to replace it with
    ``{"foo": ...}``, which validation then rejected with a type error."""
    with pytest.raises(ValueError, match="agents.defaults.model"):
        set_at({"agents": {"defaults": {"model": "glm-5.3"}}}, "agents.defaults.model.foo", "x")


def test_set_at_still_fills_an_unset_section() -> None:
    out = set_at({"a": {"b": None}}, "a.b.c", 1)
    assert out == {"a": {"b": {"c": 1}}}


def test_parse_value_decodes_json_literals() -> None:
    assert parse_value("true") is True
    assert parse_value("null") is None
    assert parse_value("42") == 42
    assert parse_value("3.14") == 3.14
    assert parse_value('"quoted"') == "quoted"
    assert parse_value('[1,2,3]') == [1, 2, 3]
    assert parse_value('{"k":"v"}') == {"k": "v"}


def test_parse_value_keeps_plain_string() -> None:
    assert parse_value("glm-5.1") == "glm-5.1"
    assert parse_value("sk-abc-123") == "sk-abc-123"


def test_mask_secrets_hides_api_keys() -> None:
    masked = mask_secrets({"providers": {"zhipu": {"api_key": "sk-x", "api_base": "https://x"}}})
    assert masked["providers"]["zhipu"]["api_key"] == "***"
    assert masked["providers"]["zhipu"]["api_base"] == "https://x"


def test_mask_secrets_passes_empty_strings() -> None:
    masked = mask_secrets({"providers": {"zhipu": {"api_key": ""}}})
    assert masked["providers"]["zhipu"]["api_key"] == ""


def test_mask_secrets_handles_lists_and_nesting() -> None:
    masked = mask_secrets({"auths": [{"token": "abc"}, {"token": "def"}]})
    assert masked == {"auths": [{"token": "***"}, {"token": "***"}]}


def test_mask_secrets_keeps_secret_references_visible() -> None:
    """A ${secret:} reference is a pointer, not a secret — show it verbatim."""
    masked = mask_secrets(
        {"providers": {"zhipu": {"api_key": "${secret:ZHIPU_API_KEY}"}}}
    )
    assert masked["providers"]["zhipu"]["api_key"] == "${secret:ZHIPU_API_KEY}"
    # A literal value is still masked.
    masked2 = mask_secrets({"providers": {"zhipu": {"api_key": "sk-literal"}}})
    assert masked2["providers"]["zhipu"]["api_key"] == "***"


def test_cli_config_set_bootstraps_when_no_config(tmp_path: Path) -> None:
    """`config set` on a fresh install creates the config instead of erroring."""
    cfg_path = tmp_path / "config.json"
    assert not cfg_path.exists()
    with patch("durin.cli.config_cmd.get_config_path", return_value=cfg_path), \
         patch("durin.config.loader.get_config_path", return_value=cfg_path):
        result = runner.invoke(
            app, ["config", "set", "agents.defaults.provider", "zhipu"]
        )
    assert result.exit_code == 0, result.output
    assert "Created config" in result.output
    from durin.config.loader import load_config

    assert load_config(cfg_path).agents.defaults.provider == "zhipu"


def test_cli_config_import_moves_plaintext_key_to_store(tmp_path: Path) -> None:
    """`config import` copies an old config and migrates its plaintext keys."""
    old = tmp_path / "old.json"
    old.write_text(
        json.dumps({"providers": {"zhipu": {"apiKey": "sk-old-plaintext"}},
                    "agents": {"defaults": {"model": "glm-5.1"}}}),
        encoding="utf-8",
    )
    cfg_path = tmp_path / "config.json"
    with patch("durin.cli.config_cmd.get_config_path", return_value=cfg_path), \
         patch("durin.config.loader.get_config_path", return_value=cfg_path):
        import durin.security.secrets as _secrets

        _secrets._STORE = None
        result = runner.invoke(app, ["config", "import", str(old)])
        assert result.exit_code == 0, result.output
        from durin.config.loader import load_config
        from durin.security.secrets import SecretStore, is_secret_ref

        cfg = load_config(cfg_path)
        assert cfg.agents.defaults.model == "glm-5.1"
        assert is_secret_ref(cfg.providers.zhipu.api_key)
        store = SecretStore(path=tmp_path / "secrets.json").load()
        assert store.get("ZHIPU_API_KEY").value == "sk-old-plaintext"
        _secrets._STORE = None


def test_validate_dict_accepts_default_config() -> None:
    data = Config().model_dump(mode="json", by_alias=True)
    assert validate_dict(data) is not None


def test_validate_dict_rejects_invalid_schema() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        validate_dict({"agents": {"defaults": {"max_tokens": "not-a-number"}}})


def test_normalize_dotted_path_snakes_camel_segments() -> None:
    # camelCase input is normalized to the snake_case canonical form.
    assert _normalize_dotted_path("providers.zhipu.apiKey") == "providers.zhipu.api_key"
    assert _normalize_dotted_path("agents.defaults.maxTokens") == "agents.defaults.max_tokens"
    assert _normalize_dotted_path("modelPresets.fast.model") == "model_presets.fast.model"
    # snake_case / single segments pass through unchanged.
    assert _normalize_dotted_path("providers.zhipu.api_key") == "providers.zhipu.api_key"
    assert _normalize_dotted_path("agents.defaults.model") == "agents.defaults.model"
    # Numeric indices are preserved.
    assert _normalize_dotted_path("agents.fallbackModels.0") == "agents.fallback_models.0"


def test_cli_config_set_api_key_via_snake_path(tmp_path: Path) -> None:
    """Setting providers.<vendor>.api_key persists as snake_case on disk."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(Config().model_dump(mode="json", by_alias=True), indent=2),
        encoding="utf-8",
    )
    with patch("durin.cli.config_cmd.get_config_path", return_value=cfg_path), \
         patch("durin.config.loader.get_config_path", return_value=cfg_path):
        result = runner.invoke(app, ["config", "set", "providers.zhipu.api_key", "sk-secret"])
    assert result.exit_code == 0, result.output
    data = json.loads(cfg_path.read_text())
    assert data["providers"]["zhipu"]["api_key"] == "sk-secret"
    # And no parallel camelCase key got planted.
    assert "apiKey" not in data["providers"]["zhipu"]


# ---------------------------------------------------------------------------
# CLI integration via typer.testing.CliRunner
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_config(tmp_path: Path):
    """Write a fresh default Config to a temp dir and point the loader at it."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(Config().model_dump(mode="json", by_alias=True), indent=2),
        encoding="utf-8",
    )
    with patch("durin.cli.config_cmd.get_config_path", return_value=cfg_path), \
         patch("durin.config.loader.get_config_path", return_value=cfg_path):
        yield cfg_path


def test_cli_config_path(temp_config: Path) -> None:
    result = runner.invoke(app, ["config", "path"])
    assert result.exit_code == 0, result.output
    # Rich wraps long paths across lines; compare with whitespace flattened.
    flat = "".join(result.output.split())
    assert str(temp_config).replace(" ", "") in flat


def test_cli_config_show_masks_secrets(temp_config: Path) -> None:
    # Plant an api_key first, then verify show masks it.
    data = json.loads(temp_config.read_text())
    data.setdefault("providers", {}).setdefault("zhipu", {})["api_key"] = "sk-secret"
    temp_config.write_text(json.dumps(data), encoding="utf-8")

    result = runner.invoke(app, ["config", "show", "providers.zhipu"])
    assert result.exit_code == 0, result.output
    assert "sk-secret" not in result.output
    assert "***" in result.output


def test_cli_config_show_raw_reveals_secrets(temp_config: Path) -> None:
    data = json.loads(temp_config.read_text())
    data.setdefault("providers", {}).setdefault("zhipu", {})["api_key"] = "sk-secret"
    temp_config.write_text(json.dumps(data), encoding="utf-8")

    result = runner.invoke(app, ["config", "show", "providers.zhipu", "--raw"])
    assert result.exit_code == 0, result.output
    assert "sk-secret" in result.output


def test_cli_config_get(temp_config: Path) -> None:
    result = runner.invoke(app, ["config", "get", "agents.defaults.model"])
    assert result.exit_code == 0, result.output
    # Default Config().agents.defaults.model varies by schema, but it's a string.
    assert result.output.strip()  # non-empty


def test_cli_config_get_missing_key_exits_1(temp_config: Path) -> None:
    result = runner.invoke(app, ["config", "get", "nope.nada"])
    assert result.exit_code == 1
    assert "No such key" in result.output


def test_cli_config_set_persists_value(temp_config: Path) -> None:
    result = runner.invoke(app, ["config", "set", "agents.defaults.model", "glm-5.1"])
    assert result.exit_code == 0, result.output
    written = json.loads(temp_config.read_text())
    assert written["agents"]["defaults"]["model"] == "glm-5.1"


def test_cli_config_set_decodes_json_literal(temp_config: Path) -> None:
    result = runner.invoke(app, ["config", "set", "agents.defaults.temperature", "0.42"])
    assert result.exit_code == 0, result.output
    written = json.loads(temp_config.read_text())
    assert written["agents"]["defaults"]["temperature"] == 0.42


def test_cli_config_set_rejects_invalid_value(temp_config: Path) -> None:
    original = temp_config.read_text()
    result = runner.invoke(app, ["config", "set", "agents.defaults.maxTokens", '"not-a-number"'])
    assert result.exit_code == 1
    assert "Validation failed" in result.output
    # File untouched after a rejected set.
    assert temp_config.read_text() == original


def test_cli_config_show_missing_config(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with patch("durin.cli.config_cmd.get_config_path", return_value=missing):
        result = runner.invoke(app, ["config", "show"])
    assert result.exit_code == 1
    assert "No config at" in result.output


def test_cli_config_edit_noop_when_unchanged(temp_config: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Editor is /usr/bin/true (or `true`): exits with no edit. We need shutil.which to find it.
    monkeypatch.setenv("EDITOR", "true")
    result = runner.invoke(app, ["config", "edit"])
    assert result.exit_code == 0, result.output
    assert "No changes" in result.output


# ---------------------------------------------------------------------------
# `config schema` — key lookup and introspection output
# ---------------------------------------------------------------------------


def test_cli_config_schema_lists_top_level_sections() -> None:
    result = runner.invoke(app, ["config", "schema"])
    assert result.exit_code == 0, result.output
    assert "agents" in result.output
    assert "config schema <dotted.key>" in result.output


def test_cli_config_schema_describes_known_key() -> None:
    result = runner.invoke(app, ["config", "schema", "agents.defaults.max_tokens"])
    assert result.exit_code == 0, result.output
    assert "agents.defaults.max_tokens" in result.output
    assert "type:" in result.output
    assert "default:" in result.output
    assert "description:" in result.output


def test_cli_config_schema_describes_aliased_section() -> None:
    """camelCase-aliased schema properties resolve from the dotted path."""
    result = runner.invoke(app, ["config", "schema", "catalog_refresh"])
    assert result.exit_code == 0, result.output
    assert "type:" in result.output


def test_cli_config_schema_rejects_unknown_key() -> None:
    result = runner.invoke(app, ["config", "schema", "agents.defaults.no_such_key"])
    assert result.exit_code == 1
    assert "No such config key" in result.output


# ---------------------------------------------------------------------------
# Keys that contain dots (model names): bracket addressing, and a path that
# does not land where it says fails loudly
# ---------------------------------------------------------------------------


@pytest.fixture
def models_config(tmp_path: Path):
    """A config with two model entries under zai_coding_plan, one of them a
    model name with a dot, and the loader pointed at it."""
    from durin.config.loader import save_config

    cfg_path = tmp_path / "config.json"
    cfg = Config()
    cfg.providers.zai_coding_plan.models = {
        "glm-5.3": ModelEntry(context_window_tokens=231_072),
        "glm-5-turbo": ModelEntry(context_window_tokens=231_072),
    }
    save_config(cfg, cfg_path)
    with patch("durin.cli.config_cmd.get_config_path", return_value=cfg_path), \
         patch("durin.config.loader.get_config_path", return_value=cfg_path):
        yield cfg_path


def _models(cfg_path: Path) -> dict:
    return json.loads(cfg_path.read_text())["providers"]["zai_coding_plan"]["models"]


def test_path_segments_split_on_dots_and_keep_bracketed_keys_whole() -> None:
    assert path_segments('providers.zai_coding_plan.models["glm-5.3"].context_window_tokens') == [
        "providers", "zai_coding_plan", "models", "glm-5.3", "context_window_tokens",
    ]
    assert path_segments("models['glm-5.3'].max_tokens") == ["models", "glm-5.3", "max_tokens"]
    assert path_segments("models[glm-5.3]") == ["models", "glm-5.3"]
    assert path_segments("agents.defaults.model") == ["agents", "defaults", "model"]


@pytest.mark.parametrize("bad", ["", ".a", "a..b", "a.", 'a["x"', "a[x]b", "a]"])
def test_path_segments_reject_a_malformed_path(bad: str) -> None:
    with pytest.raises(ValueError):
        path_segments(bad)


def test_get_at_and_set_at_address_a_key_that_contains_dots() -> None:
    data = {"models": {"glm-5.3": {"max_tokens": 1}}}
    assert get_at(data, 'models["glm-5.3"].max_tokens') == 1
    out = set_at(data, 'models["glm-5.3"].max_tokens', 2)
    assert out == {"models": {"glm-5.3": {"max_tokens": 2}}}


def test_normalize_keeps_a_map_key_as_typed() -> None:
    """Field names are case-normalized; a map key (a model name, a preset name)
    is the user's and is never rewritten."""
    assert _normalize_dotted_path("providers.minimax.models.MiniMax-M2.maxTokens") == (
        "providers.minimax.models.MiniMax-M2.max_tokens"
    )
    assert _normalize_dotted_path('providers.zaiCodingPlan.models["glm-5.3"].contextWindowTokens') == (
        'providers.zai_coding_plan.models["glm-5.3"].context_window_tokens'
    )


def test_cli_config_set_addresses_a_model_name_with_dots(models_config: Path) -> None:
    result = runner.invoke(app, [
        "config", "set", 'providers.zai_coding_plan.models["glm-5.3"].context_window_tokens', "null",
    ])
    assert result.exit_code == 0, result.output
    models = _models(models_config)
    assert models["glm-5.3"] == {}
    assert models["glm-5-turbo"] == {"context_window_tokens": 231_072}
    assert "glm-5" not in models


def test_cli_config_set_single_quoted_brackets_work_too(models_config: Path) -> None:
    result = runner.invoke(app, [
        "config", "set", "providers.zai_coding_plan.models['glm-5.3'].max_tokens", "32000",
    ])
    assert result.exit_code == 0, result.output
    assert _models(models_config)["glm-5.3"]["max_tokens"] == 32_000


def test_cli_config_set_a_dotted_model_name_without_brackets_fails_loudly(models_config: Path) -> None:
    """The dots of `glm-5.3` used to split the key: the write created an empty
    `glm-5` entry, dropped the `3` segment and printed "updated"."""
    before = models_config.read_text()
    result = runner.invoke(app, [
        "config", "set", "providers.zai_coding_plan.models.glm-5.3.context_window_tokens", "null",
    ])
    assert result.exit_code == 1
    assert "updated" not in result.output
    assert '["' in result.output  # names the bracket form to use instead
    assert models_config.read_text() == before


def test_cli_config_set_an_unknown_field_fails_loudly(temp_config: Path) -> None:
    before = temp_config.read_text()
    result = runner.invoke(app, ["config", "set", "agents.defaults.max_tokenz", "100"])
    assert result.exit_code == 1
    assert "updated" not in result.output
    assert temp_config.read_text() == before


def test_cli_config_set_a_malformed_path_fails_cleanly(temp_config: Path) -> None:
    before = temp_config.read_text()
    result = runner.invoke(app, [
        "config", "set", 'providers.zai_coding_plan.models["glm-5.3".max_tokens', "1",
    ])
    assert result.exit_code == 1
    assert "does not name a config key" in result.output
    assert temp_config.read_text() == before


def test_cli_config_set_still_creates_a_new_map_entry(temp_config: Path) -> None:
    result = runner.invoke(app, ["config", "set", "model_presets.fast.model", "glm-5-turbo"])
    assert result.exit_code == 0, result.output
    assert json.loads(temp_config.read_text())["model_presets"]["fast"]["model"] == "glm-5-turbo"


def test_cli_config_set_keeps_a_model_names_case(temp_config: Path) -> None:
    result = runner.invoke(app, ["config", "set", "providers.minimax.models.MiniMax-M2.max_tokens", "8000"])
    assert result.exit_code == 0, result.output
    models = json.loads(temp_config.read_text())["providers"]["minimax"]["models"]
    assert models == {"MiniMax-M2": {"max_tokens": 8000}}


@pytest.fixture
def fallbacks_config(tmp_path: Path):
    """A config whose default model falls back to a preset (by name) and to an
    inline model, and the loader pointed at it."""
    from durin.config.loader import save_config
    from durin.config.schema import InlineFallbackConfig, ModelPresetConfig

    cfg_path = tmp_path / "config.json"
    cfg = Config()
    cfg.model_presets["judge-cold"] = ModelPresetConfig(model="glm-5.3", provider="zai_coding_plan")
    cfg.agents.defaults.fallback_models = [
        "judge-cold",
        InlineFallbackConfig(model="glm-5-turbo", provider="zai_coding_plan", max_tokens=150_000),
    ]
    save_config(cfg, cfg_path)
    with patch("durin.cli.config_cmd.get_config_path", return_value=cfg_path), \
         patch("durin.config.loader.get_config_path", return_value=cfg_path):
        yield cfg_path


def _fallbacks(cfg_path: Path) -> list:
    return json.loads(cfg_path.read_text())["agents"]["defaults"]["fallback_models"]


@pytest.mark.parametrize("path", [
    "agents.defaults.fallback_models.1.max_tokens",
    "agents.defaults.fallback_models[1].max_tokens",
    "agents.defaults.fallbackModels.1.maxTokens",
])
def test_cli_config_set_writes_one_inline_fallback_field(fallbacks_config: Path, path: str) -> None:
    result = runner.invoke(app, ["config", "set", path, "null"])
    assert result.exit_code == 0, result.output
    assert _fallbacks(fallbacks_config) == [
        "judge-cold", {"model": "glm-5-turbo", "provider": "zai_coding_plan"},
    ]


@pytest.mark.parametrize("path", [
    "agents.defaults.fallback_models.7.model",
    "agents.defaults.fallback_models.-1.model",
    "agents.defaults.fallback_models.0.max_tokens",
])
def test_cli_config_set_refuses_a_list_item_it_cannot_write(fallbacks_config: Path, path: str) -> None:
    """Out of range, negative, or item 0 — a preset name, not a section."""
    before = fallbacks_config.read_text()
    result = runner.invoke(app, ["config", "set", path, "100"])
    assert result.exit_code == 1
    assert "does not name a config key" in result.output
    assert "Traceback" not in result.output
    assert fallbacks_config.read_text() == before


def test_normalize_renders_a_list_index_as_a_dotted_key() -> None:
    assert _normalize_dotted_path("agents.defaults.fallback_models[1].maxTokens") == (
        "agents.defaults.fallback_models.1.max_tokens"
    )


def test_cli_config_get_addresses_a_model_name_with_dots(models_config: Path) -> None:
    result = runner.invoke(app, [
        "config", "get", 'providers.zai_coding_plan.models["glm-5.3"].context_window_tokens',
    ])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "231072"


def test_cli_config_show_addresses_a_model_name_with_dots(models_config: Path) -> None:
    result = runner.invoke(app, ["config", "show", 'providers.zai_coding_plan.models["glm-5.3"]'])
    assert result.exit_code == 0, result.output
    assert "231072" in result.output


def test_cli_config_schema_describes_a_field_under_a_dotted_model_name() -> None:
    result = runner.invoke(app, [
        "config", "schema", 'providers.zai_coding_plan.models["glm-5.3"].context_window_tokens',
    ])
    assert result.exit_code == 0, result.output
    assert "type:" in result.output
