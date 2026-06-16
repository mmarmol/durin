"""Tests for McpService: status derivation, reads, writes, oauth."""
from __future__ import annotations

import pytest

from durin.agent.mcp_runtime import RawConnState
from durin.config.schema import MCPServerConfig
from durin.service.mcp import (
    McpListQuery,
    McpServerGetQuery,
    McpService,
    derive_status,
)
from durin.service.principal import Principal
from durin.service.types import ForbiddenError, NotFoundError

LOCAL = Principal.local()


@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    """Point the config loader at a fresh tmp config."""
    from durin.config.loader import save_config
    from durin.config.schema import Config

    path = tmp_path / "config.json"
    save_config(Config(), path)
    monkeypatch.setattr("durin.config.loader._current_config_path", path)
    return path


def _seed(servers: dict) -> None:
    from durin.config.loader import get_config_path, load_config, save_config

    cfg = load_config()
    cfg.tools.mcp_servers.update(servers)
    save_config(cfg, get_config_path())


class _FakeRuntime:
    def __init__(self, status: dict) -> None:
        self._status = status

    def live_status(self) -> dict:
        return self._status


def _raw(breaker_state: str, error: str | None = None) -> RawConnState:
    return RawConnState(breaker_state=breaker_state, error=error, tools=[])


def test_derive_status_disabled_wins() -> None:
    assert derive_status(
        enabled=False, oauth_required=False, oauth_authenticated=False, raw=None
    ) == ("disabled", None)
    # disabled even if a stale live connection is still around
    assert derive_status(
        enabled=False, oauth_required=False, oauth_authenticated=False, raw=_raw("closed")
    ) == ("disabled", None)


def test_derive_status_needs_auth() -> None:
    assert derive_status(
        enabled=True, oauth_required=True, oauth_authenticated=False, raw=None
    ) == ("needs_auth", None)


def test_derive_status_connected() -> None:
    assert derive_status(
        enabled=True, oauth_required=False, oauth_authenticated=False, raw=_raw("closed")
    ) == ("connected", None)
    # oauth satisfied + live
    assert derive_status(
        enabled=True, oauth_required=True, oauth_authenticated=True, raw=_raw("closed")
    ) == ("connected", None)


def test_derive_status_failed_carries_error() -> None:
    assert derive_status(
        enabled=True, oauth_required=False, oauth_authenticated=False, raw=_raw("open", "boom")
    ) == ("failed", "boom")


def test_derive_status_connecting() -> None:
    # half-open breaker is a probe-in-progress
    assert derive_status(
        enabled=True, oauth_required=False, oauth_authenticated=False, raw=_raw("half-open")
    ) == ("connecting", None)
    # enabled + authed but no live connection yet (coming up / runtime absent)
    assert derive_status(
        enabled=True, oauth_required=False, oauth_authenticated=False, raw=None
    ) == ("connecting", None)


# --- list + get -----------------------------------------------------------


async def test_list_returns_summaries_with_live_status(config_path) -> None:
    _seed(
        {
            "a": MCPServerConfig(url="https://a/mcp", enabled=True),
            "b": MCPServerConfig(command="npx", args=["x"], enabled=False),
        }
    )
    runtime = _FakeRuntime(
        {"a": RawConnState(breaker_state="closed", error=None, tools=[("mcp_a_t", "T")])}
    )
    res = await McpService(mcp_runtime=runtime).list(McpListQuery(), LOCAL)

    by = {s.name: s for s in res.servers}
    assert by["a"].status == "connected"
    assert by["a"].tool_count == 1
    assert by["a"].transport == "streamableHttp"
    assert by["a"].target == "https://a/mcp"
    assert by["b"].status == "disabled"
    assert by["b"].transport == "stdio"
    assert by["b"].target == "npx x"


async def test_list_requires_mcp_read(config_path) -> None:
    with pytest.raises(ForbiddenError):
        await McpService().list(McpListQuery(), Principal.remote("t", set()))


async def test_get_unknown_raises_not_found(config_path) -> None:
    with pytest.raises(NotFoundError) as exc:
        await McpService().get(McpServerGetQuery(name="nope"), LOCAL)
    assert exc.value.details == {"name": "nope"}


async def test_get_returns_detail_with_tools(config_path) -> None:
    _seed({"a": MCPServerConfig(url="https://a/mcp")})
    runtime = _FakeRuntime(
        {"a": RawConnState(breaker_state="closed", error=None, tools=[("mcp_a_t", "desc")])}
    )
    detail = await McpService(mcp_runtime=runtime).get(McpServerGetQuery(name="a"), LOCAL)

    assert detail.name == "a"
    assert detail.status == "connected"
    assert detail.tools[0].name == "mcp_a_t"
    assert detail.tools[0].description == "desc"
    assert detail.config.url == "https://a/mcp"


async def test_oauth_server_without_token_is_needs_auth(config_path, monkeypatch) -> None:
    _seed({"o": MCPServerConfig(url="https://o/mcp", oauth=True)})

    class _NoToken:
        def __init__(self, *a, **k) -> None:
            pass

        async def get_tokens(self):
            return None

    monkeypatch.setattr("durin.agent.tools.mcp_oauth.SecretsTokenStorage", _NoToken)

    res = await McpService().list(McpListQuery(), LOCAL)
    o = next(s for s in res.servers if s.name == "o")
    assert o.oauth_required is True
    assert o.oauth_authenticated is False
    assert o.status == "needs_auth"
