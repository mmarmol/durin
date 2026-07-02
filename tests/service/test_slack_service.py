"""Unit tests for SlackService (manifest, token test, pairing management)."""

from __future__ import annotations

import pytest

pytest.importorskip("slack_sdk")

from durin.pairing import store
from durin.service.channels_slack import (
    SLACK_BOT_EVENTS,
    SLACK_BOT_SCOPES,
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

    def __init__(self, token: str):
        self._token = token

    async def auth_test(self):
        if self._token.startswith("xoxb-good"):
            return {"user": "durin", "team": "Acme"}
        raise RuntimeError("invalid_auth xoxb-secret-value")

    async def apps_connections_open(self):
        if self._token.startswith("xapp-good"):
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
    assert set(r.json()) == {"pending", "approved"}


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
