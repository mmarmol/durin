"""Tests for the per-channel field schema returned by ConfigService.channels_list."""

import durin.config.loader as _loader
from durin.service.config import ChannelsListQuery, ConfigService
from durin.service.principal import Principal


def _principal():
    return Principal.local()


async def test_email_returns_typed_field_schema():
    svc = ConfigService()
    result = await svc.channels_list(query=ChannelsListQuery(), principal=_principal())
    email = next(c for c in result.channels if c["name"] == "email")
    by_name = {f["name"]: f for f in email["fields"]}
    assert by_name["imap_host"]["type"] == "string"
    assert by_name["imap_port"]["type"] == "int"
    assert by_name["imap_use_ssl"]["type"] == "bool"
    assert by_name["allow_from"]["type"] == "string_list"
    assert by_name["imap_password"]["type"] == "secret"
    assert by_name["imap_password"]["secret"] is True
    assert by_name["imap_host"]["group"] == "imap"


async def test_websocket_always_on_when_webui_enabled(monkeypatch):
    cfg = _loader.load_config()
    cfg.gateway.webui_enabled = True
    monkeypatch.setattr(_loader, "load_config", lambda *a, **kw: cfg)

    svc = ConfigService()
    result = await svc.channels_list(query=ChannelsListQuery(), principal=_principal())
    ws = next(c for c in result.channels if c["name"] == "websocket")
    assert ws["always_on"] is True
    # enabled reflects the literal config value (websocket not explicitly enabled
    # in the test config); always_on is what the webui uses to show "always active"
    assert ws["enabled"] is False
    assert ws["description"]
    token = next(f for f in ws["fields"] if f["name"] == "token")
    assert token["type"] == "secret"
    # Only the token is surfaced — host, ssl paths, and token_issue_secret
    # (a signing secret) must NOT appear in the UI schema.
    names = {f["name"] for f in ws["fields"]}
    assert names == {"token"}


async def test_slack_returns_typed_field_schema():
    import pytest

    pytest.importorskip("slack_sdk")
    svc = ConfigService()
    result = await svc.channels_list(query=ChannelsListQuery(), principal=_principal())
    slack = next(c for c in result.channels if c["name"] == "slack")
    by_name = {f["name"]: f for f in slack["fields"]}
    assert by_name["bot_token"]["secret"] is True
    assert by_name["app_token"]["secret"] is True
    assert by_name["allow_from"]["type"] == "string_list"
    assert by_name["dm_enabled"]["type"] == "bool"
    assert by_name["group_policy"]["group"] == "access"
    assert by_name["group_policy"]["type"] == "select"
    assert by_name["group_policy"]["options"] == ["open", "mention", "allowlist"]
    assert by_name["reply_in_thread"]["group"] == "behavior"
    # Emoji + thread_context_limit stay config-file-only (no group/secret marker).
    assert set(by_name) == {
        "bot_token",
        "app_token",
        "allow_from",
        "dm_enabled",
        "group_policy",
        "group_allow_from",
        "reply_in_thread",
        "include_thread_context",
        "streaming",
        "thread_auto_follow",
    }
