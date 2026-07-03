"""Unit tests for SlackService (manifest, token test, pairing management)."""

from __future__ import annotations

import pytest

pytest.importorskip("slack_sdk")

from durin.pairing import store
from durin.service.channels_slack import (
    SLACK_BOT_EVENTS,
    SLACK_BOT_SCOPES,
    SlackChannelsListQuery,
    SlackJoinChannelCommand,
    SlackManifestQuery,
    SlackPairingApproveCommand,
    SlackPairingDenyCommand,
    SlackPairingListQuery,
    SlackPairingRevokeCommand,
    SlackService,
    SlackTestCommand,
)
from durin.service.principal import Principal

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


async def test_manifest_provisions_scopes_events_and_socket_mode():
    res = await SlackService().manifest(SlackManifestQuery(), Principal.local())
    m = res.manifest
    assert m["oauth_config"]["scopes"]["bot"] == SLACK_BOT_SCOPES
    assert m["settings"]["event_subscriptions"]["bot_events"] == SLACK_BOT_EVENTS
    assert m["settings"]["socket_mode_enabled"] is True
    assert m["settings"]["interactivity"]["is_enabled"] is True
    assert m["display_information"]["name"] == "durin"


async def test_manifest_respects_custom_name():
    res = await SlackService().manifest(SlackManifestQuery(name="my-agent"), Principal.local())
    assert res.manifest["display_information"]["name"] == "my-agent"
    assert res.manifest["features"]["bot_user"]["display_name"] == "my-agent"


async def test_manifest_scopes_cover_channel_api_calls():
    """Every Web API family used by durin/channels/slack.py needs its scope."""
    for scope in ("chat:write", "files:read", "files:write", "reactions:write",
                  "im:history", "im:write", "channels:read", "users:read",
                  "app_mentions:read"):
        assert scope in SLACK_BOT_SCOPES


# ---------------------------------------------------------------------------
# Token test
# ---------------------------------------------------------------------------


class _FakeWebClient:
    """Stands in for AsyncWebClient; behavior keyed on the token value."""

    def __init__(self, token: str = ""):
        self._token = token

    async def auth_test(self):
        if self._token.startswith("xoxb-good"):
            return {"user": "durin", "team": "Acme"}
        raise RuntimeError("invalid_auth xoxb-secret-value")

    async def apps_connections_open(self, *, app_token: str):
        # Signature mirrors slack_sdk: app_token is a required keyword — a
        # no-arg fake previously hid a TypeError shipped to production.
        if app_token.startswith("xapp-good"):
            return {"ok": True}
        raise RuntimeError("invalid_auth xapp-secret-value")


@pytest.fixture
def fake_web_client(monkeypatch):
    monkeypatch.setattr("slack_sdk.web.async_client.AsyncWebClient", _FakeWebClient)


async def test_token_test_ok(fake_web_client):
    res = await SlackService().test(
        SlackTestCommand(bot_token="xoxb-good", app_token="xapp-good"), Principal.local()
    )
    assert res.ok is True
    assert res.bot_user == "durin"
    assert res.team == "Acme"
    assert res.bot_error is None and res.app_error is None


async def test_token_test_reports_per_token_errors(fake_web_client):
    res = await SlackService().test(
        SlackTestCommand(bot_token="xoxb-bad", app_token="xapp-good"), Principal.local()
    )
    assert res.ok is False
    assert res.bot_error == "RuntimeError"
    assert res.app_error is None

    res = await SlackService().test(
        SlackTestCommand(bot_token="xoxb-good", app_token="xapp-bad"), Principal.local()
    )
    assert res.ok is False
    assert res.bot_error is None
    assert res.app_error == "RuntimeError"


async def test_token_test_error_does_not_expose_tokens(fake_web_client):
    res = await SlackService().test(
        SlackTestCommand(bot_token="xoxb-bad", app_token="xapp-bad"), Principal.local()
    )
    for field in (res.bot_error, res.app_error):
        assert field is not None
        assert "secret-value" not in field


async def test_token_test_requires_at_least_one_token():
    res = await SlackService().test(SlackTestCommand(), Principal.local())
    assert res.ok is False


async def test_token_test_persists_nothing(monkeypatch, fake_web_client):
    """test() must never write to the secret store or the config file."""

    def _forbidden(*a, **kw):
        raise AssertionError("test endpoint must not persist anything")

    monkeypatch.setattr("durin.security.secrets.store_secret", _forbidden)
    monkeypatch.setattr("durin.config.loader.save_config", _forbidden)
    res = await SlackService().test(
        SlackTestCommand(bot_token="xoxb-good", app_token="xapp-good"), Principal.local()
    )
    assert res.ok is True


# ---------------------------------------------------------------------------
# Pairing endpoints
# ---------------------------------------------------------------------------


async def test_pairing_list_filters_to_slack():
    store.generate_code("telegram", "tg-user-1")
    code = store.generate_code("slack", "U123")
    svc = SlackService()
    listed = await svc.pairing(SlackPairingListQuery(), Principal.local())
    assert all(p["channel"] == "slack" for p in listed.pending)
    assert any(p["code"] == code for p in listed.pending)


async def test_pairing_approve_and_revoke():
    code = store.generate_code("slack", "U456")
    svc = SlackService()
    res = await svc.pairing_approve(SlackPairingApproveCommand(code=code), Principal.local())
    assert res.ok is True and res.channel == "slack" and res.sender_id == "U456"
    listed = await svc.pairing(SlackPairingListQuery(), Principal.local())
    assert "U456" in listed.approved

    revoked = await svc.pairing_revoke(
        SlackPairingRevokeCommand(sender_id="U456"), Principal.local()
    )
    assert revoked.ok is True
    after = await svc.pairing(SlackPairingListQuery(), Principal.local())
    assert "U456" not in after.approved


async def test_pairing_deny():
    code = store.generate_code("slack", "U789")
    svc = SlackService()
    res = await svc.pairing_deny(SlackPairingDenyCommand(code=code), Principal.local())
    assert res.ok is True
    listed = await svc.pairing(SlackPairingListQuery(), Principal.local())
    assert not any(p["code"] == code for p in listed.pending)


async def test_pairing_unknown_inputs():
    svc = SlackService()
    assert (await svc.pairing_approve(SlackPairingApproveCommand(code="ZZZZ"), Principal.local())).ok is False
    assert (await svc.pairing_deny(SlackPairingDenyCommand(code="ZZZZ"), Principal.local())).ok is False
    assert (await svc.pairing_revoke(SlackPairingRevokeCommand(sender_id="nobody"), Principal.local())).ok is False


def test_slack_routes_over_asgi(tmp_path):
    """Manifest + pairing routes dispatch through the real Starlette app."""
    from starlette.testclient import TestClient

    from durin.api.asgi import build_api_app
    from durin.security.api_tokens import ApiTokenStore
    from durin.service.auth import AuthService
    from durin.service.registry import ServiceRegistry

    auth = AuthService(store=ApiTokenStore(path=tmp_path / "tokens.json"))
    reg = ServiceRegistry()
    reg.register("slack", SlackService())
    reg.register("auth", auth)
    client = TestClient(
        build_api_app(reg, auth=auth, static_token="tok"),
        raise_server_exceptions=False,
    )
    headers = {"Authorization": "Bearer tok"}

    r = client.get("/api/v1/channels/slack/manifest", headers=headers)
    assert r.status_code == 200
    assert r.json()["manifest"]["settings"]["socket_mode_enabled"] is True

    r = client.get("/api/v1/channels/slack/manifest?name=my-agent", headers=headers)
    assert r.json()["manifest"]["display_information"]["name"] == "my-agent"

    r = client.get("/api/v1/channels/slack/pairing", headers=headers)
    assert r.status_code == 200
    assert set(r.json()) == {"pending", "approved", "names"}


def test_gateway_registry_registers_slack_like_the_catalog():
    """Slack must be registered in BOTH wiring (live gateway) and catalog
    (OpenAPI/tooling) registries — drift means silent 405s on one surface."""
    from durin.service.catalog import build_catalog_registry
    from durin.service.wiring import build_service_registry

    wiring = build_service_registry(
        config=None, session_manager=None, cron_service=None, bus=None
    )
    wnames = {b.service_name for b in wiring.routes}
    assert "slack" in wnames
    assert {b.service_name for b in build_catalog_registry().routes} == wnames
    assert any(b.spec.path.endswith("/slack/manifest") for b in wiring.routes)
    assert any(b.spec.path.endswith("/slack/test") for b in wiring.routes)


async def test_manifest_enables_dm_messages_tab():
    """Without app_home.messages_tab_enabled Slack blocks DMs to the bot."""
    res = await SlackService().manifest(SlackManifestQuery(), Principal.local())
    app_home = res.manifest["features"]["app_home"]
    assert app_home["messages_tab_enabled"] is True
    assert app_home["messages_tab_read_only_enabled"] is False


async def test_token_test_detects_swapped_tokens():
    res = await SlackService().test(
        SlackTestCommand(bot_token="xapp-1-A1-x", app_token="xoxb-123-x"), Principal.local()
    )
    assert res.ok is False
    assert res.bot_error == "swapped_tokens"
    assert res.app_error == "swapped_tokens"


async def test_token_test_rejects_wrong_prefixes_without_calling_slack(fake_web_client):
    res = await SlackService().test(
        SlackTestCommand(bot_token="xoxp-user-token", app_token="xapp-good"), Principal.local()
    )
    assert res.ok is False
    assert res.bot_error == "expected_bot_token"
    assert res.app_error is None

    res = await SlackService().test(
        SlackTestCommand(bot_token="xoxb-good", app_token="xoxa-something"), Principal.local()
    )
    assert res.ok is False
    assert res.bot_error is None
    assert res.app_error == "expected_app_token"


# ---------------------------------------------------------------------------
# Channel listing / joining
# ---------------------------------------------------------------------------


class _FakeChannelsClient:
    joined: list[str] = []

    def __init__(self, token: str):
        self._token = token

    async def conversations_list(self, **kwargs):
        return {
            "channels": [
                {"id": "C1", "name": "general", "is_member": False, "is_private": False},
                {"id": "C2", "name": "dev", "is_member": True, "is_private": False},
                {"id": "G1", "name": "secret", "is_member": False, "is_private": True},
            ],
            "response_metadata": {"next_cursor": ""},
        }

    async def conversations_join(self, *, channel: str):
        if channel.startswith("G"):
            raise RuntimeError("method_not_supported_for_channel_type")
        _FakeChannelsClient.joined.append(channel)
        return {"ok": True}


@pytest.fixture
def configured_channels_client(monkeypatch):
    _FakeChannelsClient.joined = []
    monkeypatch.setattr("slack_sdk.web.async_client.AsyncWebClient", _FakeChannelsClient)
    monkeypatch.setattr(
        "durin.service.channels_slack._configured_bot_token", lambda: "xoxb-configured"
    )


async def test_channels_list_reports_membership(configured_channels_client):
    res = await SlackService().channels(SlackChannelsListQuery(), Principal.local())
    assert res.ok is True
    by_id = {c["id"]: c for c in res.channels}
    assert by_id["C2"]["is_member"] is True
    assert by_id["G1"]["is_private"] is True
    # Members sort first, then by name.
    assert res.channels[0]["id"] == "C2"


async def test_channels_list_requires_configured_token(monkeypatch):
    monkeypatch.setattr(
        "durin.service.channels_slack._configured_bot_token", lambda: None
    )
    res = await SlackService().channels(SlackChannelsListQuery(), Principal.local())
    assert res.ok is False
    assert res.error == "not_configured"


async def test_join_channel_public_ok_private_error(configured_channels_client):
    res = await SlackService().join_channel(
        SlackJoinChannelCommand(channel_id="C1"), Principal.local()
    )
    assert res.ok is True
    assert _FakeChannelsClient.joined == ["C1"]

    res = await SlackService().join_channel(
        SlackJoinChannelCommand(channel_id="G1"), Principal.local()
    )
    assert res.ok is False
    assert res.error == "RuntimeError"


async def test_manifest_includes_channels_join_scope():
    assert "channels:join" in SLACK_BOT_SCOPES


async def test_pairing_list_resolves_display_names(monkeypatch):
    import durin.service.channels_slack as mod

    class _FakeUsersClient:
        def __init__(self, token: str = ""):
            pass

        async def users_info(self, *, user: str):
            if user == "U_NAMED":
                return {"user": {"profile": {"display_name": "Marcelo"}, "name": "mmarmol"}}
            raise RuntimeError("user_not_found")

    mod._NAME_CACHE.clear()
    monkeypatch.setattr("slack_sdk.web.async_client.AsyncWebClient", _FakeUsersClient)
    monkeypatch.setattr(
        "durin.service.channels_slack._configured_bot_token", lambda: "xoxb-configured"
    )
    code = store.generate_code("slack", "U_NAMED")
    store.approve_code(code)
    store.generate_code("slack", "U_GHOST")

    listed = await SlackService().pairing(SlackPairingListQuery(), Principal.local())
    assert listed.names.get("U_NAMED") == "Marcelo"
    # Unresolvable ids simply stay un-named; the UI falls back to the raw id.
    assert "U_GHOST" not in listed.names


async def test_pairing_list_without_token_returns_no_names(monkeypatch):
    import durin.service.channels_slack as mod

    mod._NAME_CACHE.clear()
    monkeypatch.setattr(
        "durin.service.channels_slack._configured_bot_token", lambda: None
    )
    store.generate_code("slack", "U_ANY")
    listed = await SlackService().pairing(SlackPairingListQuery(), Principal.local())
    assert listed.names == {}
