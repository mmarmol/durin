"""SP1: OAuthService — unit tests (no HTTP, no channel, no real OAuth calls)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from durin.providers import codex_device_auth as cda
from durin.service.oauth import (
    OAuthDisconnectCommand,
    OAuthPollQuery,
    OAuthService,
    OAuthStartCommand,
    OAuthStartLoopbackCommand,
    OAuthStatusQuery,
)
from durin.service.principal import Principal, Scope
from durin.service.types import ForbiddenError, UnavailableError, ValidationFailedError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeSessionInfo:
    email: str | None
    plan: str | None
    source: str


@dataclass
class _FakePollResult:
    status: str
    error: str | None = None
    token: Any = None


def _local() -> Principal:
    return Principal.local()


def _remote_read() -> Principal:
    return Principal.remote("t", frozenset({Scope.SETTINGS_READ.value}))


def _remote_write() -> Principal:
    return Principal.remote("t", frozenset({Scope.SETTINGS_WRITE.value}))


def _remote_none() -> Principal:
    return Principal.remote("t", frozenset())


@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    """Point the config loader at a fresh tmp config (needed for the
    OpenRouter gateway-path tests, which resolve ``gateway.public_url``)."""
    from durin.config.loader import save_config
    from durin.config.schema import Config

    path = tmp_path / "config.json"
    save_config(Config(), path)
    monkeypatch.setattr("durin.config.loader._current_config_path", path)
    return path


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


async def test_status_connected(monkeypatch):
    info = _FakeSessionInfo(email="u@x.com", plan="pro", source="durin")
    monkeypatch.setattr(
        cda, "existing_codex_session", lambda: info
    )
    result = await OAuthService().status(OAuthStatusQuery(is_local=False), _local())
    assert result.connected is True
    assert result.email == "u@x.com"
    assert result.plan == "pro"
    assert result.source == "durin"
    assert result.can_loopback is False


async def test_status_disconnected(monkeypatch):
    monkeypatch.setattr(
        cda, "existing_codex_session", lambda: None
    )
    result = await OAuthService().status(OAuthStatusQuery(is_local=False), _local())
    assert result.connected is False
    assert result.email is None


async def test_status_can_loopback_local(monkeypatch):
    monkeypatch.setattr(
        cda, "existing_codex_session", lambda: None
    )
    result = await OAuthService().status(OAuthStatusQuery(is_local=True), _local())
    assert result.can_loopback is True


async def test_status_can_loopback_remote(monkeypatch):
    monkeypatch.setattr(
        cda, "existing_codex_session", lambda: None
    )
    result = await OAuthService().status(OAuthStatusQuery(is_local=False), _local())
    assert result.can_loopback is False


async def test_status_requires_read_scope(monkeypatch):
    monkeypatch.setattr(
        cda, "existing_codex_session", lambda: None
    )
    with pytest.raises(ForbiddenError):
        await OAuthService().status(OAuthStatusQuery(), _remote_none())


# ---------------------------------------------------------------------------
# start_loopback
# ---------------------------------------------------------------------------


async def test_start_loopback_local_returns_url(monkeypatch):
    monkeypatch.setattr(
        cda, "start_loopback_login",
        lambda **_kw: "https://auth.openai.com/oauth/authorize?x=1",
    )
    result = await OAuthService().start_loopback(
        OAuthStartLoopbackCommand(is_local=True), _local()
    )
    assert result.authorize_url.startswith("https://auth.openai.com/oauth/authorize")


async def test_start_loopback_non_local_raises_forbidden(monkeypatch):
    with pytest.raises(ForbiddenError, match="loopback unavailable"):
        await OAuthService().start_loopback(
            OAuthStartLoopbackCommand(is_local=False), _local()
        )


async def test_start_loopback_upstream_failure_raises_unavailable(monkeypatch):
    def _fail(**_kw):
        raise RuntimeError("port in use")

    monkeypatch.setattr(cda, "start_loopback_login", _fail)
    with pytest.raises(UnavailableError, match="loopback login failed"):
        await OAuthService().start_loopback(
            OAuthStartLoopbackCommand(is_local=True), _local()
        )


async def test_start_loopback_requires_write_scope(monkeypatch):
    with pytest.raises(ForbiddenError):
        await OAuthService().start_loopback(
            OAuthStartLoopbackCommand(is_local=True), _remote_read()
        )


# ---------------------------------------------------------------------------
# start (device code)
# ---------------------------------------------------------------------------


@dataclass
class _FakeChallenge:
    user_code: str
    verification_uri: str
    device_auth_id: str
    interval: int
    expires_in: int


async def test_start_returns_challenge(monkeypatch):
    ch = _FakeChallenge(
        user_code="WXYZ-1",
        verification_uri="https://auth.openai.com/codex/device",
        device_auth_id="dev_1",
        interval=5,
        expires_in=900,
    )
    monkeypatch.setattr(cda, "request_device_code", lambda: ch)
    result = await OAuthService().start(OAuthStartCommand(), _local())
    assert result.user_code == "WXYZ-1"
    assert result.device_auth_id == "dev_1"
    assert result.interval == 5
    assert result.expires_in == 900


async def test_start_upstream_failure_raises_unavailable(monkeypatch):
    def _fail():
        raise RuntimeError("network error")

    monkeypatch.setattr(cda, "request_device_code", _fail)
    with pytest.raises(UnavailableError, match="device code request failed"):
        await OAuthService().start(OAuthStartCommand(), _local())


async def test_start_requires_write_scope():
    with pytest.raises(ForbiddenError):
        await OAuthService().start(OAuthStartCommand(), _remote_read())


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------


async def test_poll_pending(monkeypatch):
    monkeypatch.setattr(
        cda, "poll_once",
        lambda did, uc: _FakePollResult(status="pending"),
    )
    result = await OAuthService().poll(
        OAuthPollQuery(device_auth_id="dev_1", user_code="WXYZ-1"), _local()
    )
    assert result.status == "pending"
    assert result.connected is None
    assert result.error is None


async def test_poll_ok_includes_session(monkeypatch):
    info = _FakeSessionInfo(email="u@x.com", plan="pro", source="durin")
    monkeypatch.setattr(
        cda, "existing_codex_session", lambda: info
    )
    monkeypatch.setattr(
        cda, "poll_once",
        lambda did, uc: _FakePollResult(status="ok"),
    )
    result = await OAuthService().poll(
        OAuthPollQuery(device_auth_id="dev_1", user_code="WXYZ-1"), _local()
    )
    assert result.status == "ok"
    assert result.connected is True
    assert result.email == "u@x.com"


async def test_poll_error_includes_message(monkeypatch):
    monkeypatch.setattr(
        cda, "poll_once",
        lambda did, uc: _FakePollResult(status="error", error="expired"),
    )
    result = await OAuthService().poll(
        OAuthPollQuery(device_auth_id="dev_1", user_code="WXYZ-1"), _local()
    )
    assert result.status == "error"
    assert result.error == "expired"


async def test_poll_missing_params_raises_validation_error():
    with pytest.raises(ValidationFailedError, match="device_auth_id and user_code"):
        await OAuthService().poll(
            OAuthPollQuery(device_auth_id="  ", user_code="WXYZ-1"), _local()
        )


async def test_poll_requires_read_scope():
    with pytest.raises(ForbiddenError):
        await OAuthService().poll(
            OAuthPollQuery(device_auth_id="dev_1", user_code="WXYZ-1"), _remote_none()
        )


# ---------------------------------------------------------------------------
# disconnect
# ---------------------------------------------------------------------------


async def test_disconnect_returns_disconnected_status(monkeypatch):
    disconnected = []
    monkeypatch.setattr(
        cda, "disconnect", lambda: disconnected.append(True)
    )
    monkeypatch.setattr(
        cda, "existing_codex_session", lambda: None
    )
    result = await OAuthService().disconnect(OAuthDisconnectCommand(), _local())
    assert result.connected is False
    assert disconnected == [True]


async def test_disconnect_requires_write_scope(monkeypatch):
    with pytest.raises(ForbiddenError):
        await OAuthService().disconnect(OAuthDisconnectCommand(), _remote_read())


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------

from durin.providers import openrouter_oauth as oro  # noqa: E402
from durin.service.oauth import (  # noqa: E402
    OpenRouterDisconnectCommand,
    OpenRouterStartLoopbackCommand,
    OpenRouterStatusQuery,
)


async def test_openrouter_status_connected(monkeypatch):
    monkeypatch.setattr(
        oro, "key_status",
        lambda: oro.OpenRouterKeyStatus(connected=True, api_key_hint="sk-o…v1"),
    )
    result = await OAuthService().openrouter_status(
        OpenRouterStatusQuery(is_local=True), _local()
    )
    assert result.connected is True
    assert result.api_key_hint == "sk-o…v1"
    assert result.can_loopback is True


async def test_openrouter_status_disconnected_remote(monkeypatch):
    monkeypatch.setattr(
        oro, "key_status", lambda: oro.OpenRouterKeyStatus(connected=False)
    )
    result = await OAuthService().openrouter_status(
        OpenRouterStatusQuery(is_local=False), _remote_read()
    )
    assert result.connected is False
    assert result.can_loopback is False


async def test_openrouter_status_requires_read_scope():
    with pytest.raises(ForbiddenError):
        await OAuthService().openrouter_status(OpenRouterStatusQuery(), _remote_none())


async def test_openrouter_start_loopback_local_returns_url(monkeypatch):
    monkeypatch.setattr(
        oro, "start_loopback_login",
        lambda **_kw: "https://openrouter.ai/auth?callback_url=x",
    )
    result = await OAuthService().openrouter_start_loopback(
        OpenRouterStartLoopbackCommand(is_local=True), _local()
    )
    assert result.authorize_url.startswith("https://openrouter.ai/auth")


async def test_openrouter_start_loopback_non_local_forbidden():
    with pytest.raises(ForbiddenError, match="loopback unavailable"):
        await OAuthService().openrouter_start_loopback(
            OpenRouterStartLoopbackCommand(is_local=False), _local()
        )


async def test_openrouter_start_loopback_failure_unavailable(monkeypatch):
    def _fail(**_kw):
        raise RuntimeError("bind failed")

    monkeypatch.setattr(oro, "start_loopback_login", _fail)
    with pytest.raises(UnavailableError, match="loopback login failed"):
        await OAuthService().openrouter_start_loopback(
            OpenRouterStartLoopbackCommand(is_local=True), _local()
        )


async def test_openrouter_disconnect(monkeypatch):
    calls = []
    monkeypatch.setattr(oro, "disconnect", lambda: calls.append(True) or True)
    monkeypatch.setattr(
        oro, "key_status", lambda: oro.OpenRouterKeyStatus(connected=False)
    )
    result = await OAuthService().openrouter_disconnect(
        OpenRouterDisconnectCommand(), _local()
    )
    assert result.connected is False
    assert calls == [True]


async def test_openrouter_disconnect_requires_write_scope():
    with pytest.raises(ForbiddenError):
        await OAuthService().openrouter_disconnect(
            OpenRouterDisconnectCommand(), _remote_read()
        )


# ---------------------------------------------------------------------------
# OpenRouter — gateway callback path (remote one-click connect)
# ---------------------------------------------------------------------------


def _set_public_url(url: str | None) -> None:
    from durin.config.loader import get_config_path, load_config, save_config

    cfg = load_config()
    cfg.gateway.public_url = url
    save_config(cfg, get_config_path())


async def test_openrouter_start_gateway_via_origin_no_is_local_required(
    config_path, monkeypatch
):
    """No config public_url, but a validated origin resolves a base: the
    gateway-callback path is used and a non-local principal is NOT forbidden
    (unlike the loopback-only branch)."""
    captured: dict = {}

    async def fake_start_gateway_login(base: str, **_kw) -> str:
        captured["base"] = base
        return "https://openrouter.ai/auth?callback_url=gw"

    monkeypatch.setattr(oro, "start_gateway_login", fake_start_gateway_login)

    result = await OAuthService().openrouter_start_loopback(
        OpenRouterStartLoopbackCommand(
            is_local=False, origin="https://durin.example.com"
        ),
        _remote_write(),
    )
    assert result.authorize_url == "https://openrouter.ai/auth?callback_url=gw"
    assert captured["base"] == "https://durin.example.com"


async def test_openrouter_start_gateway_config_public_url_wins_over_origin(
    config_path, monkeypatch
):
    captured: dict = {}

    async def fake_start_gateway_login(base: str, **_kw) -> str:
        captured["base"] = base
        return "https://openrouter.ai/auth?callback_url=gw"

    monkeypatch.setattr(oro, "start_gateway_login", fake_start_gateway_login)
    _set_public_url("https://durin.tail9e5f5d.ts.net")

    result = await OAuthService().openrouter_start_loopback(
        OpenRouterStartLoopbackCommand(
            is_local=False, origin="https://durin.example.com"
        ),
        _remote_write(),
    )
    assert result.authorize_url == "https://openrouter.ai/auth?callback_url=gw"
    assert captured["base"] == "https://durin.tail9e5f5d.ts.net"


async def test_openrouter_start_gateway_failure_raises_unavailable(
    config_path, monkeypatch
):
    async def _fail(base: str, **_kw) -> str:
        raise RuntimeError("callback bind failed")

    monkeypatch.setattr(oro, "start_gateway_login", _fail)

    with pytest.raises(UnavailableError, match="gateway login failed"):
        await OAuthService().openrouter_start_loopback(
            OpenRouterStartLoopbackCommand(
                is_local=False, origin="https://durin.example.com"
            ),
            _remote_write(),
        )


async def test_openrouter_start_no_base_non_local_still_forbidden(config_path):
    """No config public_url and no (or invalid) origin: the gateway path
    doesn't apply, so the original local-only loopback gate stands."""
    with pytest.raises(ForbiddenError, match="loopback unavailable"):
        await OAuthService().openrouter_start_loopback(
            OpenRouterStartLoopbackCommand(is_local=False, origin=""), _local()
        )
