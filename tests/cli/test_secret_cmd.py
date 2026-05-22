"""Tests for the `durin secret` CLI group."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from durin.cli.commands import app
from durin.security.secrets import SecretStore

runner = CliRunner()


@pytest.fixture
def secrets_path(tmp_path):
    """Point the store at a temp file by faking the config path."""
    config_path = tmp_path / "config.json"
    with patch("durin.config.loader.get_config_path", return_value=config_path):
        yield tmp_path / "secrets.json"


def test_secret_set_stores_value_from_hidden_prompt(secrets_path) -> None:
    result = runner.invoke(
        app,
        ["secret", "set", "ATLASSIAN_WORK", "--service", "atlassian",
         "--account", "work", "--scope", "exec,skill:*"],
        input="tok-secret-123\n",
    )
    assert result.exit_code == 0, result.output
    assert "Stored secret" in result.output
    entry = SecretStore(path=secrets_path).load().get("ATLASSIAN_WORK")
    assert entry is not None
    assert entry.value == "tok-secret-123"
    assert entry.service == "atlassian"
    assert entry.scope == ["exec", "skill:*"]


def test_secret_set_rejects_bad_name(secrets_path) -> None:
    result = runner.invoke(
        app, ["secret", "set", "bad-name", "--service", "x"], input="v\n"
    )
    assert result.exit_code == 1
    assert "Invalid name" in result.output


def test_secret_list_masks_values(secrets_path) -> None:
    store = SecretStore(path=secrets_path)
    store.put("OPENAI_MAIN", value="sk-supersecret-value", service="provider:openai")
    store.save()
    result = runner.invoke(app, ["secret", "list"])
    assert result.exit_code == 0
    assert "OPENAI_MAIN" in result.output
    # The raw value must never appear; only a masked tail.
    assert "sk-supersecret-value" not in result.output
    assert "alue" in result.output  # last-4 of the value


def test_secret_show_hides_value_by_default_reveals_with_flag(secrets_path) -> None:
    store = SecretStore(path=secrets_path)
    store.put("K", value="plaintext-secret", service="atlassian")
    store.save()

    hidden = runner.invoke(app, ["secret", "show", "K"])
    assert hidden.exit_code == 0
    assert "plaintext-secret" not in hidden.output

    shown = runner.invoke(app, ["secret", "show", "K", "--reveal"])
    assert shown.exit_code == 0
    assert "plaintext-secret" in shown.output


def test_secret_rm(secrets_path) -> None:
    store = SecretStore(path=secrets_path)
    store.put("K", value="v", service="x")
    store.save()
    result = runner.invoke(app, ["secret", "rm", "K"])
    assert result.exit_code == 0
    assert SecretStore(path=secrets_path).load().get("K") is None


def test_secret_rm_unknown_errors(secrets_path) -> None:
    result = runner.invoke(app, ["secret", "rm", "GHOST"])
    assert result.exit_code == 1
    assert "No secret named" in result.output


def test_secret_grant_and_revoke(secrets_path) -> None:
    store = SecretStore(path=secrets_path)
    store.put("K", value="v", service="x", scope=["exec"])
    store.save()

    granted = runner.invoke(app, ["secret", "grant", "K", "--to", "skill:deploy"])
    assert granted.exit_code == 0
    assert SecretStore(path=secrets_path).load().get("K").scope == ["exec", "skill:deploy"]

    revoked = runner.invoke(app, ["secret", "revoke", "K", "--from", "exec"])
    assert revoked.exit_code == 0
    assert SecretStore(path=secrets_path).load().get("K").scope == ["skill:deploy"]
