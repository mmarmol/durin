"""Tests for the agent-facing secret tools — list_secrets, request_secret."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from durin.agent.tools.secrets import ListSecretsTool, RequestSecretTool
from durin.security.secrets import SecretStore


@pytest.fixture
def store_at(tmp_path):
    config_path = tmp_path / "config.json"
    import durin.security.secrets as _secrets

    _secrets._STORE = None
    with patch("durin.config.loader.get_config_path", return_value=config_path):
        yield tmp_path / "secrets.json"
    _secrets._STORE = None


async def test_list_secrets_empty(store_at) -> None:
    out = await ListSecretsTool().execute()
    assert "No secrets" in out


async def test_list_secrets_shows_metadata_never_values(store_at) -> None:
    store = SecretStore(path=store_at)
    store.put("ATLASSIAN_WORK", value="tok-supersecret-value", service="atlassian",
              account="work", scope=["exec"], description="work jira")
    store.save()

    out = await ListSecretsTool().execute()
    assert "ATLASSIAN_WORK" in out
    assert "atlassian" in out
    assert "work jira" in out
    assert "$ATLASSIAN_WORK" in out  # exec-usable hint
    assert "tok-supersecret-value" not in out  # value never shown


async def test_request_secret_existing_exec_scoped(store_at) -> None:
    store = SecretStore(path=store_at)
    store.put("GH", value="ghp-secret", service="github", scope=["exec"])
    store.save()

    out = await RequestSecretTool().execute(name="GH", service="github")
    assert "already exists" in out
    assert "$GH" in out
    assert "ghp-secret" not in out


async def test_request_secret_existing_not_exec_scoped(store_at) -> None:
    store = SecretStore(path=store_at)
    store.put("GH", value="ghp-secret", service="github", scope=["provider:x"])
    store.save()

    out = await RequestSecretTool().execute(name="GH", service="github")
    assert "already exists" in out
    assert "grant GH --to exec" in out


async def test_request_secret_same_service_other_name(store_at) -> None:
    store = SecretStore(path=store_at)
    store.put("GH_PERSONAL", value="ghp-1", service="github", scope=["exec"])
    store.save()

    out = await RequestSecretTool().execute(name="GH_WORK", service="github")
    assert "GH_PERSONAL" in out
    assert "github" in out


async def test_request_secret_new_yields_with_command(store_at) -> None:
    out = await RequestSecretTool().execute(
        name="STRIPE_KEY", service="stripe", purpose="charge a card"
    )
    assert "presented to the user" in out
    assert "durin secret set STRIPE_KEY --service stripe --scope exec" in out
    assert "charge a card" in out


async def test_request_secret_sets_pending_metadata(store_at, tmp_path) -> None:
    """request_secret registers a pending_secret_request payload so channels
    can render/serialize the prompt (user_payloads contract)."""
    from durin.agent.tools.context import RequestContext
    from durin.agent.user_payloads import PENDING_SECRET_KEY
    from durin.session.manager import SessionManager

    sm = SessionManager(tmp_path / "sessions")
    tool = RequestSecretTool(sessions=sm)
    tool.set_context(RequestContext(
        channel="cli", chat_id="d", session_key="cli:d", metadata={},
    ))
    await tool.execute(name="GH_TOKEN", service="github", purpose="push")
    payload = sm.get_or_create("cli:d").metadata.get(PENDING_SECRET_KEY)
    assert payload == {"name": "GH_TOKEN", "service": "github", "purpose": "push"}


async def test_request_secret_without_context_still_returns_block(store_at) -> None:
    out = await RequestSecretTool().execute(name="GH_TOKEN", service="github")
    assert "presented to the user" in out
    assert "durin secret set GH_TOKEN" in out


async def test_request_secret_rejects_bad_name(store_at) -> None:
    out = await RequestSecretTool().execute(name="bad-name", service="x")
    assert "not a valid secret name" in out


async def test_secret_tools_are_discovered_by_the_loader() -> None:
    """Both tools must be picked up by the package-scanning loader."""
    from durin.agent.tools.loader import ToolLoader

    discovered = {cls.__name__ for cls in ToolLoader().discover()}
    assert "ListSecretsTool" in discovered
    assert "RequestSecretTool" in discovered

async def test_request_secret_existing_hints_update_flag(store_at) -> None:
    store = SecretStore(path=store_at)
    store.put("GH", value="x", service="github", scope=["exec"])
    store.save()

    out = await RequestSecretTool().execute(name="GH", service="github")
    assert "already exists" in out
    assert "update=true" in out


async def test_request_secret_update_yields_replace_block(store_at) -> None:
    store = SecretStore(path=store_at)
    store.put("GH", value="x", service="github", scope=["exec", "channel:telegram"])
    store.save()

    out = await RequestSecretTool().execute(name="GH", service="github", update=True)
    assert "REPLACE" in out
    assert "durin secret set GH" in out
    assert "--service" not in out          # metadata-safe command
    assert "already exists" not in out     # webui already-stored detection
    assert "is not stored" not in out      # webui create-mode detection


async def test_request_secret_update_missing_degrades_to_create(store_at) -> None:
    out = await RequestSecretTool().execute(name="NEW", service="github", update=True)
    assert "is not stored" in out
    assert "--service github" in out


async def test_request_secret_update_sets_pending_flag(store_at, tmp_path) -> None:
    """update=True marks the pending payload — but only when the secret exists."""
    from durin.agent.tools.context import RequestContext
    from durin.agent.user_payloads import PENDING_SECRET_KEY
    from durin.session.manager import SessionManager

    store = SecretStore(path=store_at)
    store.put("GH", value="x", service="github", scope=["exec"])
    store.save()

    sm = SessionManager(tmp_path / "sessions")
    tool = RequestSecretTool(sessions=sm)
    tool.set_context(RequestContext(
        channel="cli", chat_id="d", session_key="cli:d", metadata={},
    ))
    await tool.execute(name="GH", service="github", purpose="rotate", update=True)
    payload = sm.get_or_create("cli:d").metadata.get(PENDING_SECRET_KEY)
    assert payload == {
        "name": "GH", "service": "github", "purpose": "rotate", "update": True,
    }

    # Degraded case: update=True but the secret does not exist → create flow,
    # no update flag on the payload.
    await tool.execute(name="MISSING", service="github", update=True)
    payload = sm.get_or_create("cli:d").metadata.get(PENDING_SECRET_KEY)
    assert payload == {"name": "MISSING", "service": "github", "purpose": ""}
