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
    set_at,
    validate_dict,
)
from durin.config.schema import Config

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


def test_normalize_dotted_path_camelizes_snake_segments() -> None:
    assert _normalize_dotted_path("providers.zhipu.api_key") == "providers.zhipu.apiKey"
    assert _normalize_dotted_path("agents.defaults.max_tokens") == "agents.defaults.maxTokens"
    assert _normalize_dotted_path("model_presets.fast.model") == "modelPresets.fast.model"
    # Pure camelCase / single segments pass through.
    assert _normalize_dotted_path("agents.defaults.model") == "agents.defaults.model"
    # Numeric indices are preserved.
    assert _normalize_dotted_path("agents.fallback_models.0") == "agents.fallbackModels.0"


def test_cli_config_set_api_key_via_snake_path(tmp_path: Path) -> None:
    """Setting providers.<vendor>.api_key (snake_case) must persist as apiKey."""
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
    assert data["providers"]["zhipu"]["apiKey"] == "sk-secret"
    # And no parallel snake_case key got planted.
    assert "api_key" not in data["providers"]["zhipu"]


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
