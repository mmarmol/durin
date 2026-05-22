"""Phase-2 tests: secret redaction + scoped exec injection."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from durin.security.secrets import SecretRedactor, SecretStore


@pytest.fixture
def store_at(tmp_path):
    """Isolated secret store: redirect the config path and reset the cache."""
    config_path = tmp_path / "config.json"
    from durin.security import secrets as _secrets

    _secrets._STORE = None
    with patch("durin.config.loader.get_config_path", return_value=config_path):
        yield tmp_path / "secrets.json"
    _secrets._STORE = None


# -- redaction ---------------------------------------------------------------


def test_redactor_replaces_value_with_named_marker() -> None:
    r = SecretRedactor({"ATLASSIAN_WORK": "tok-abcdefgh1234"})
    out = r.redact_text("the token is tok-abcdefgh1234 ok")
    assert "tok-abcdefgh1234" not in out
    assert "«redacted:ATLASSIAN_WORK»" in out


def test_redactor_ignores_short_values() -> None:
    """Values under 8 chars are too collision-prone to redact."""
    r = SecretRedactor({"SHORT": "abc123"})
    assert not r.active
    assert r.redact_text("abc123 stays") == "abc123 stays"


def test_redactor_handles_list_of_blocks() -> None:
    r = SecretRedactor({"K": "supersecretvalue99"})
    blocks = [
        {"type": "text", "text": "leak: supersecretvalue99"},
        {"type": "image", "url": "http://x"},
    ]
    out = r.redact(blocks)
    assert out[0]["text"] == "leak: «redacted:K»"
    assert out[1] == {"type": "image", "url": "http://x"}


def test_redactor_inactive_when_no_secrets() -> None:
    r = SecretRedactor({})
    assert not r.active
    assert r.redact("anything at all") == "anything at all"


def test_redact_secrets_uses_the_store(store_at) -> None:
    from durin.security.secrets import redact_secrets

    store = SecretStore(path=store_at)
    store.put("API", value="live-key-abcdefgh", service="atlassian")
    store.save()

    out = redact_secrets("calling with live-key-abcdefgh now")
    assert "live-key-abcdefgh" not in out
    assert "«redacted:API»" in out


# -- scoped exec injection ---------------------------------------------------


def test_exec_scoped_secrets_only_returns_exec_scope(store_at) -> None:
    store = SecretStore(path=store_at)
    store.put("DEPLOY_TOKEN", value="deploy-secret-1", service="atlassian",
              scope=["exec"])
    store.put("OPENAI_KEY", value="provider-secret-1", service="provider:openai",
              scope=["provider:openai"])
    store.save()

    from durin.agent.tools.shell import ExecTool

    injected = ExecTool._exec_scoped_secrets()
    assert injected == {"DEPLOY_TOKEN": "deploy-secret-1"}


def test_build_env_includes_exec_secrets_excludes_provider(store_at) -> None:
    store = SecretStore(path=store_at)
    store.put("DEPLOY_TOKEN", value="deploy-secret-1", service="x", scope=["exec"])
    store.put("OPENAI_KEY", value="provider-secret-1", service="provider:openai",
              scope=["provider:openai"])
    store.save()

    from durin.agent.tools.shell import ExecTool

    env = ExecTool()._build_env()
    assert env.get("DEPLOY_TOKEN") == "deploy-secret-1"
    assert "OPENAI_KEY" not in env  # provider-scoped never reaches exec
