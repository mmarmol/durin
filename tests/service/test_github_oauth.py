"""OAuthService — GitHub device-flow endpoints (no HTTP, no real OAuth calls)."""

from __future__ import annotations

import pytest

import durin.security.github_device_auth as gda
from durin.service.oauth import (
    GithubDisconnectCommand,
    GithubPollQuery,
    GithubStartCommand,
    GithubStatusQuery,
    OAuthService,
)
from durin.service.principal import Principal, Scope
from durin.service.types import ForbiddenError, UnavailableError, ValidationFailedError


def _local() -> Principal:
    return Principal.local()


def _remote_none() -> Principal:
    return Principal.remote("t", frozenset())


# --- status ------------------------------------------------------------------


async def test_github_status_maps_live_probe(monkeypatch):
    st = gda.Status(
        connected=True, reachable=True, login="marcelo",
        scopes="read:user", rate_remaining=4982, rate_limit=5000,
    )
    monkeypatch.setattr(gda, "github_status", lambda: st)
    res = await OAuthService().github_status(GithubStatusQuery(), _local())
    assert res.connected and res.reachable
    assert res.login == "marcelo" and res.scopes == "read:user"
    assert res.rate_remaining == 4982 and res.rate_limit == 5000


async def test_github_status_not_connected(monkeypatch):
    monkeypatch.setattr(gda, "github_status", lambda: gda.Status(connected=False, reachable=False))
    res = await OAuthService().github_status(GithubStatusQuery(), _local())
    assert res.connected is False
    assert res.login is None


async def test_github_status_requires_read_scope(monkeypatch):
    monkeypatch.setattr(gda, "github_status", lambda: gda.Status(connected=False, reachable=False))
    with pytest.raises(ForbiddenError):
        await OAuthService().github_status(GithubStatusQuery(), _remote_none())


# --- start (device flow) -----------------------------------------------------


async def test_github_start_minimal_scope_by_default(monkeypatch):
    seen = {}

    def _req(*, scope):
        seen["scope"] = scope
        return gda.Challenge(
            flow_id="FID", user_code="AB-CD", verification_uri="https://gh/dev",
            verification_uri_complete="https://gh/dev?c=AB-CD", interval=5, expires_in=900,
        )

    monkeypatch.setattr(gda, "request_device_code", _req)
    res = await OAuthService().github_start(GithubStartCommand(), _local())
    assert res.flow_id == "FID" and res.user_code == "AB-CD"
    assert res.verification_uri_complete.endswith("AB-CD")
    assert seen["scope"] == gda.DEFAULT_SCOPE  # minimal privilege by default


async def test_github_start_private_escalates_scope(monkeypatch):
    seen = {}

    def _req(*, scope):
        seen["scope"] = scope
        return gda.Challenge(
            flow_id="F", user_code="U", verification_uri="u",
            verification_uri_complete="u", interval=5, expires_in=900,
        )

    monkeypatch.setattr(gda, "request_device_code", _req)
    await OAuthService().github_start(GithubStartCommand(private=True), _local())
    assert seen["scope"] == gda.PRIVATE_REPO_SCOPE


async def test_github_start_upstream_failure_raises_unavailable(monkeypatch):
    def _fail(*, scope):
        raise RuntimeError("network down")

    monkeypatch.setattr(gda, "request_device_code", _fail)
    with pytest.raises(UnavailableError, match="device code request failed"):
        await OAuthService().github_start(GithubStartCommand(), _local())


async def test_github_start_requires_write_scope():
    with pytest.raises(ForbiddenError):
        await OAuthService().github_start(GithubStartCommand(), _remote_none())


# --- poll --------------------------------------------------------------------


async def test_github_poll_authorized_reports_connected(monkeypatch):
    monkeypatch.setattr(gda, "poll_flow", lambda fid: gda.Exchange(status="authorized", access_token="gho"))
    monkeypatch.setattr(
        gda, "github_status", lambda: gda.Status(connected=True, reachable=True, login="marcelo")
    )
    res = await OAuthService().github_poll(GithubPollQuery(flow_id="FID"), _local())
    assert res.status == "authorized"
    assert res.connected is True and res.login == "marcelo"


async def test_github_poll_pending_passthrough(monkeypatch):
    monkeypatch.setattr(gda, "poll_flow", lambda fid: gda.Exchange(status="pending"))
    res = await OAuthService().github_poll(GithubPollQuery(flow_id="FID"), _local())
    assert res.status == "pending"
    assert res.connected is None


async def test_github_poll_empty_flow_id_is_validation_error():
    with pytest.raises(ValidationFailedError):
        await OAuthService().github_poll(GithubPollQuery(flow_id="  "), _local())


# --- disconnect --------------------------------------------------------------


async def test_github_disconnect_forgets_and_reports_disconnected(monkeypatch):
    called = {}
    monkeypatch.setattr(gda, "forget_github_token", lambda: called.setdefault("forgot", True))
    res = await OAuthService().github_disconnect(GithubDisconnectCommand(), _local())
    assert res.connected is False
    assert called.get("forgot") is True
