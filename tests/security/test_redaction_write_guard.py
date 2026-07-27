"""Config writes must never persist a redaction marker.

Every tool result is redacted before it enters the model context, so an
agent that reads a config file sees `«redacted»` (pattern layer) or
`«redacted:NAME»` (value layer) where a credential was. Writing that view
back replaces a working credential with a marker — silently, and only
discovered when the channel or provider stops authenticating.

Sparing `${secret:…}` references in the pattern layer removes the common
trigger; this guard closes the class. It is scoped to credential-shaped
keys so free-text config (persona descriptions, prompts) can still say the
word.
"""

from __future__ import annotations

import pytest

from durin.config.loader import load_config, save_config
from durin.config.schema import Config
from durin.security.secrets import RedactedValueError, find_redacted_credentials


def _config_with_slack(bot_token: str) -> Config:
    cfg = Config()
    cfg.channels.slack = {"enabled": True, "bot_token": bot_token}
    return cfg


# -- the detector -------------------------------------------------------------


def test_finds_pattern_marker_in_credential_field() -> None:
    data = {"channels": {"slack": {"bot_token": "«redacted»"}}}
    assert find_redacted_credentials(data) == ["channels.slack.bot_token"]


def test_finds_value_marker_in_credential_field() -> None:
    data = {"providers": {"openrouter": {"api_key": "«redacted:OPENROUTER_API_KEY»"}}}
    assert find_redacted_credentials(data) == ["providers.openrouter.api_key"]


def test_finds_every_offending_path() -> None:
    data = {
        "channels": {
            "slack": {"bot_token": "«redacted»", "app_token": "«redacted»"},
            "telegram": {"token": "${secret:TELEGRAM_TOKEN}"},
        }
    }
    assert find_redacted_credentials(data) == [
        "channels.slack.app_token",
        "channels.slack.bot_token",
    ]


def test_secret_ref_is_not_a_marker() -> None:
    data = {"channels": {"slack": {"bot_token": "${secret:SLACK_BOT_TOKEN}"}}}
    assert find_redacted_credentials(data) == []


def test_free_text_may_contain_the_word() -> None:
    """Only credential-shaped keys are guarded — prose is left alone."""
    data = {"personas": {"auditor": {"description": "Explains why «redacted» appears"}}}
    assert find_redacted_credentials(data) == []


def test_walks_into_lists() -> None:
    data = {"mcp": {"servers": [{"name": "a"}, {"api_key": "«redacted»"}]}}
    assert find_redacted_credentials(data) == ["mcp.servers[1].api_key"]


# -- the guard at the write choke point --------------------------------------


def test_save_config_rejects_redacted_credential(tmp_path) -> None:
    path = tmp_path / "config.json"
    with pytest.raises(RedactedValueError) as excinfo:
        save_config(_config_with_slack("«redacted»"), path)
    assert "channels.slack.bot_token" in str(excinfo.value)
    assert not path.exists(), "nothing may be written when the guard trips"


def test_save_config_rejects_value_layer_marker(tmp_path) -> None:
    path = tmp_path / "config.json"
    with pytest.raises(RedactedValueError):
        save_config(_config_with_slack("«redacted:SLACK_BOT_TOKEN»"), path)


def test_save_config_accepts_secret_ref(tmp_path) -> None:
    path = tmp_path / "config.json"
    save_config(_config_with_slack("${secret:SLACK_BOT_TOKEN}"), path)
    reloaded = load_config(path)
    assert reloaded.channels.slack["bot_token"] == "${secret:SLACK_BOT_TOKEN}"


def test_save_config_leaves_a_good_config_untouched_on_reject(tmp_path) -> None:
    """The guard must not clobber what is already on disk."""
    path = tmp_path / "config.json"
    save_config(_config_with_slack("${secret:SLACK_BOT_TOKEN}"), path)
    with pytest.raises(RedactedValueError):
        save_config(_config_with_slack("«redacted»"), path)
    assert load_config(path).channels.slack["bot_token"] == "${secret:SLACK_BOT_TOKEN}"
