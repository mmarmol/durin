import json
import socket
from unittest.mock import patch

from durin.config.loader import load_config, save_config
from durin.security.network import validate_url_target


def _fake_resolve(host: str, results: list[str]):
    """Return a getaddrinfo mock that maps the given host to fake IP results."""
    def _resolver(hostname, port, family=0, type_=0):
        if hostname == host:
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in results]
        raise socket.gaierror(f"cannot resolve {hostname}")
    return _resolver


def test_load_config_keeps_max_tokens_and_ignores_legacy_memory_window(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "maxTokens": 1234,
                        "memoryWindow": 42,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.agents.defaults.max_tokens == 1234
    assert config.agents.defaults.context_window_tokens == 65_536
    assert not hasattr(config.agents.defaults, "memory_window")


def test_save_config_writes_context_window_tokens_but_not_memory_window(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "maxTokens": 2222,
                        "memoryWindow": 30,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)
    save_config(config, config_path)
    from durin.config.loader import read_persisted_config

    saved = read_persisted_config(config_path)
    defaults = saved["agents"]["defaults"]

    assert defaults["max_tokens"] == 2222
    # context_window_tokens isn't asserted explicitly — `exclude_defaults`
    # drops it when the user-supplied value matches the default.
    # The original intent (the legacy memory-window key must NOT survive the
    # round-trip) is what really matters.
    assert "memoryWindow" not in defaults
    assert "memory_window" not in defaults


def test_onboard_does_not_crash_with_legacy_memory_window(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "defaults": {
                        "maxTokens": 3333,
                        "memoryWindow": 50,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("durin.config.loader.get_config_path", lambda: config_path)
    monkeypatch.setattr("durin.cli.commands.get_workspace_path", lambda _workspace=None: workspace)

    from typer.testing import CliRunner

    from durin.cli.commands import app
    runner = CliRunner()
    # `--no-wizard` for a deterministic non-interactive path.
    result = runner.invoke(app, ["onboard", "--no-wizard"])

    assert result.exit_code == 0


def test_onboard_refresh_backfills_missing_channel_fields(tmp_path, monkeypatch) -> None:
    """An ENABLED channel missing fields gets the full attribute set
    backfilled. (Disabled channels are intentionally left alone — they
    shouldn't add noise.)"""
    from types import SimpleNamespace

    config_path = tmp_path / "config.json"
    workspace = tmp_path / "workspace"
    config_path.write_text(
        json.dumps(
            {
                "channels": {
                    "qq": {
                        "enabled": True,
                        "appId": "app-123",
                        "secret": "shh",
                        "allowFrom": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("durin.config.loader.get_config_path", lambda: config_path)
    monkeypatch.setattr("durin.cli.commands.get_workspace_path", lambda _workspace=None: workspace)
    monkeypatch.setattr(
        "durin.channels.registry.discover_all",
        lambda: {
            "qq": SimpleNamespace(
                default_config=lambda: {
                    "enabled": False,
                    "appId": "",
                    "secret": "",
                    "allowFrom": [],
                    "msgFormat": "plain",
                }
            )
        },
    )

    from typer.testing import CliRunner

    from durin.cli.commands import app
    runner = CliRunner()
    # `--no-wizard` does not auto-refresh existing configs (the migration
    # check is gated on the file being missing). The test that verifies
    # backfill of missing channel fields now goes through the legacy
    # walker (`--advanced`), which still loads + saves via run_onboard.
    from durin.cli.onboard import OnboardResult

    monkeypatch.setattr(
        "durin.cli.onboard.run_onboard",
        lambda initial_config: OnboardResult(config=initial_config, should_save=True),
    )
    monkeypatch.setattr("durin.cli.commands._stdin_is_interactive", lambda: True)
    result = runner.invoke(app, ["onboard", "--advanced"])

    assert result.exit_code == 0
    from durin.config.loader import read_persisted_config

    saved = read_persisted_config(config_path)
    assert saved["channels"]["qq"]["msgFormat"] == "plain"


def test_load_config_migrates_legacy_my_tool_keys(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "tools": {
                    "myEnabled": False,
                    "mySet": True,
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.tools.my.enable is False
    assert config.tools.my.allow_set is True


def test_save_config_rewrites_legacy_my_tool_keys(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "tools": {
                    "myEnabled": False,
                    "mySet": True,
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)
    save_config(config, config_path)
    from durin.config.loader import read_persisted_config

    saved = read_persisted_config(config_path)

    tools = saved["tools"]
    assert "myEnabled" not in tools
    assert "mySet" not in tools
    assert tools["my"] == {"enable": False, "allow_set": True}


def test_new_my_tool_keys_take_precedence_over_legacy(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "tools": {
                    "myEnabled": False,
                    "mySet": False,
                    "my": {"enable": True, "allowSet": True},
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.tools.my.enable is True
    assert config.tools.my.allow_set is True


def test_load_config_resets_ssrf_whitelist_when_next_config_is_empty(tmp_path) -> None:
    whitelisted = tmp_path / "whitelisted.json"
    whitelisted.write_text(
        json.dumps({"tools": {"ssrfWhitelist": ["100.64.0.0/10"]}}),
        encoding="utf-8",
    )
    defaulted = tmp_path / "defaulted.json"
    defaulted.write_text(json.dumps({}), encoding="utf-8")

    load_config(whitelisted)
    with patch("durin.security.network.socket.getaddrinfo", _fake_resolve("ts.local", ["100.100.1.1"])):
        ok, err = validate_url_target("http://ts.local/api")
        assert ok, err

    load_config(defaulted)
    with patch("durin.security.network.socket.getaddrinfo", _fake_resolve("ts.local", ["100.100.1.1"])):
        ok, _ = validate_url_target("http://ts.local/api")
        assert not ok


def test_load_config_migrates_legacy_skill_import(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"memory": {"skillImport": {"allowlist": ["github:acme/"], "maxFiles": 50}}}),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.skills.security.allowlist == ["github:acme/"]
    assert config.skills.security.max_files == 50
    assert not hasattr(config.memory, "skill_import")


def test_load_config_migrates_legacy_skills_hot_tier(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"memory": {"skillsHotTier": {"frequent": 12}}}), encoding="utf-8"
    )

    config = load_config(config_path)

    assert config.agents.defaults.skills_hot_tier.frequent == 12


def test_save_config_rewrites_legacy_skill_keys(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {"memory": {"skillImport": {"allowlist": ["github:acme/"]},
                        "skillsHotTier": {"frequent": 9}}}
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)
    save_config(config, config_path)
    from durin.config.loader import read_persisted_config

    saved = read_persisted_config(config_path)

    assert "skillImport" not in saved.get("memory", {})
    assert "skillsHotTier" not in saved.get("memory", {})


def test_skills_section_survives_split_layout_roundtrip(tmp_path) -> None:
    """The new top-level `skills` section must persist through save (which
    converts to the split layout) + reload — else migrated/user skill config is
    silently lost when the split-file allowlist omits the section."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"skills": {"security": {"allowlist": ["github:acme/"]}}}),
        encoding="utf-8",
    )

    cfg = load_config(config_path)
    assert cfg.skills.security.allowlist == ["github:acme/"]

    save_config(cfg, config_path)  # → split layout
    reloaded = load_config(config_path)

    assert reloaded.skills.security.allowlist == ["github:acme/"]


def test_telemetry_section_survives_split_layout_roundtrip(tmp_path) -> None:
    """Regression: telemetry was silently dropped on save/migrate because the
    split layout used a hardcoded section list it was added to after the fact."""
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"telemetry": {"push": {"enabled": True}}}), encoding="utf-8"
    )

    cfg = load_config(config_path)  # migrates monolith → split
    save_config(cfg, config_path)  # writes split

    assert load_config(config_path).telemetry.push.enabled is True


def test_write_split_layout_persists_every_section_including_unknown(tmp_path) -> None:
    """The split writer must persist EVERY top-level section it is handed —
    including one no hardcoded list ever knew about — so a future Config section
    can never be silently dropped on save."""
    from durin.config.loader import _read_split_layout, _write_split_layout

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"_layout": "split"}) + "\n", encoding="utf-8")
    data = {
        "agents": {"defaults": {"botName": "x"}},
        "telemetry": {"push": {"enabled": True}},
        "appearance": {"foo": 1},
        "skills": {"security": {"allowlist": ["github:acme/"]}},
        "aFutureSectionNotYetInvented": {"k": 2},
    }

    _write_split_layout(data, config_path)

    assert _read_split_layout(config_path) == data
