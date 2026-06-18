"""DURIN_HOME — dev/daily data-root separation.

A single env var relocates durin's entire home data root (config, secrets,
sessions, workspace, …) so a dev (editable) install and a daily (pipx) install
no longer share state. Unset → behavior identical to today (~/.durin). Set →
NO resolved path may fall back to ~/.durin.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def home_unset(monkeypatch):
    monkeypatch.delenv("DURIN_HOME", raising=False)
    # The config-path module global must not shadow the default branch.
    monkeypatch.setattr("durin.config.loader._current_config_path", None)


@pytest.fixture()
def home_set(monkeypatch, tmp_path):
    root = tmp_path / "durin-dev"
    monkeypatch.setenv("DURIN_HOME", str(root))
    monkeypatch.setattr("durin.config.loader._current_config_path", None)
    return root


# ---------------------------------------------------------------------------
# durin_home() helper
# ---------------------------------------------------------------------------


def test_durin_home_defaults_to_dot_durin(home_unset):
    from durin.config.home import durin_home

    assert durin_home() == Path.home() / ".durin"


def test_durin_home_reads_env(home_set):
    from durin.config.home import durin_home

    assert durin_home() == home_set


def test_durin_home_expands_user(monkeypatch):
    monkeypatch.setenv("DURIN_HOME", "~/durin-dev")
    from durin.config.home import durin_home

    assert durin_home() == Path.home() / "durin-dev"


def test_durin_home_blank_env_treated_as_unset(monkeypatch):
    monkeypatch.setenv("DURIN_HOME", "   ")
    from durin.config.home import durin_home

    assert durin_home() == Path.home() / ".durin"


# ---------------------------------------------------------------------------
# Unset → identical to today
# ---------------------------------------------------------------------------


def test_unset_paths_identical_to_today(home_unset):
    from durin.config.loader import get_config_path
    from durin.config.paths import (
        get_bridge_install_dir,
        get_cli_history_path,
        get_legacy_sessions_dir,
        get_workspace_path,
        is_default_workspace,
    )

    home = Path.home() / ".durin"
    assert get_config_path() == home / "config.json"
    assert get_workspace_path() == home / "workspace"
    assert get_cli_history_path() == home / "history" / "cli_history"
    assert get_bridge_install_dir() == home / "bridge"
    assert get_legacy_sessions_dir() == home / "sessions"
    assert is_default_workspace(None) is True


# ---------------------------------------------------------------------------
# Set → everything relocates, nothing falls back to ~/.durin
# ---------------------------------------------------------------------------


def test_set_relocates_core_paths(home_set):
    from durin.config.loader import get_config_path
    from durin.config.paths import (
        get_bridge_install_dir,
        get_cli_history_path,
        get_data_dir,
        get_legacy_sessions_dir,
        get_workspace_path,
        is_default_workspace,
    )

    assert get_config_path() == home_set / "config.json"
    assert get_data_dir() == home_set
    assert get_workspace_path() == home_set / "workspace"
    assert get_cli_history_path() == home_set / "history" / "cli_history"
    assert get_bridge_install_dir() == home_set / "bridge"
    assert get_legacy_sessions_dir() == home_set / "sessions"
    # is_default_workspace tracks DURIN_HOME, not the literal ~/.durin
    assert is_default_workspace(None) is True
    assert is_default_workspace(home_set / "workspace") is True


def test_set_relocates_derived_credential_stores(home_set):
    from durin.security.api_tokens import ApiTokenStore
    from durin.security.secrets import _default_secrets_path

    assert _default_secrets_path() == home_set / "secrets.json"
    # ApiTokenStore defaults to get_data_dir()/api_tokens.json
    assert ApiTokenStore()._path == home_set / "api_tokens.json"


def test_set_workspace_default_factory_follows_home(home_set):
    from durin.config.schema import AgentDefaults

    ws = Path(AgentDefaults().workspace).expanduser()
    assert ws == home_set / "workspace"


def test_set_no_path_falls_back_to_dot_durin(home_set):
    from durin.config.loader import get_config_path
    from durin.config.paths import (
        get_bridge_install_dir,
        get_cli_history_path,
        get_data_dir,
        get_legacy_sessions_dir,
        get_workspace_path,
    )
    from durin.security.secrets import _default_secrets_path

    legacy_root = (Path.home() / ".durin").resolve()
    resolved = [
        get_config_path(),
        get_data_dir(),
        get_workspace_path(),
        get_cli_history_path(),
        get_bridge_install_dir(),
        get_legacy_sessions_dir(),
        _default_secrets_path(),
    ]
    for p in resolved:
        rp = Path(p).resolve()
        assert legacy_root not in rp.parents and rp != legacy_root, f"{p} fell back to ~/.durin"
