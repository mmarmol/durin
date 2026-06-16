"""SP1: OAuthService — unit tests (no HTTP, no channel, no real OAuth calls)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

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


def _remote_none() -> Principal:
    return Principal.remote("t", frozenset())


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


async def test_status_connected(monkeypatch):
    info = _FakeSessionInfo(email="u@x.com", plan="pro", source="durin")
    monkeypatch.setattr(
        "durin.providers.codex_device_auth.existing_codex_session", lambda: info
    )
    result = await OAuthService().status(OAuthStatusQuery(is_local=False), _local())
    assert result.connected is True
    assert result.email == "u@x.com"
    assert result.plan == "pro"
    assert result.source == "durin"
    assert result.can_loopback is False


async def test_status_disconnected(monkeypatch):
    monkeypatch.setattr(
        "durin.providers.codex_device_auth.existing_codex_session", lambda: None
    )
    result = await OAuthService().status(OAuthStatusQuery(is_local=False), _local())
    assert result.connected is False
    assert result.email is None


async def test_status_can_loopback_local(monkeypatch):
    monkeypatch.setattr(
        "durin.providers.codex_device_auth.existing_codex_session", lambda: None
    )
    result = await OAuthService().status(OAuthStatusQuery(is_local=True), _local())
    assert result.can_loopback is True


async def test_status_can_loopback_remote(monkeypatch):
    monkeypatch.setattr(
        "durin.providers.codex_device_auth.existing_codex_session", lambda: None
    )
    result = await OAuthService().status(OAuthStatusQuery(is_local=False), _local())
    assert result.can_loopback is False


async def test_status_requires_read_scope(monkeypatch):
    monkeypatch.setattr(
        "durin.providers.codex_device_auth.existing_codex_session", lambda: None
    )
    with pytest.raises(ForbiddenError):
        await OAuthService().status(OAuthStatusQuery(), _remote_none())


# ---------------------------------------------------------------------------
# start_loopback
# ---------------------------------------------------------------------------


async def test_start_loopback_local_returns_url(monkeypatch):
    monkeypatch.setattr(
        "durin.providers.codex_device_auth.start_loopback_login",
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

    monkeypatch.setattr("durin.providers.codex_device_auth.start_loopback_login", _fail)
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
    monkeypatch.setattr("durin.providers.codex_device_auth.request_device_code", lambda: ch)
    result = await OAuthService().start(OAuthStartCommand(), _local())
    assert result.user_code == "WXYZ-1"
    assert result.device_auth_id == "dev_1"
    assert result.interval == 5
    assert result.expires_in == 900


async def test_start_upstream_failure_raises_unavailable(monkeypatch):
    def _fail():
        raise RuntimeError("network error")

    monkeypatch.setattr("durin.providers.codex_device_auth.request_device_code", _fail)
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
        "durin.providers.codex_device_auth.poll_once",
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
        "durin.providers.codex_device_auth.existing_codex_session", lambda: info
    )
    monkeypatch.setattr(
        "durin.providers.codex_device_auth.poll_once",
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
        "durin.providers.codex_device_auth.poll_once",
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
        "durin.providers.codex_device_auth.disconnect", lambda: disconnected.append(True)
    )
    monkeypatch.setattr(
        "durin.providers.codex_device_auth.existing_codex_session", lambda: None
    )
    result = await OAuthService().disconnect(OAuthDisconnectCommand(), _local())
    assert result.connected is False
    assert disconnected == [True]


async def test_disconnect_requires_write_scope(monkeypatch):
    with pytest.raises(ForbiddenError):
        await OAuthService().disconnect(OAuthDisconnectCommand(), _remote_read())
