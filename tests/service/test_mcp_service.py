"""Tests for McpService: status derivation, reads, writes, oauth."""
from __future__ import annotations

import pytest

from durin.agent.mcp_runtime import RawConnState
from durin.config.schema import MCPServerConfig
from durin.service.mcp import (
    McpListQuery,
    McpOauthLoginCommand,
    McpServerGetQuery,
    McpServerNameCommand,
    McpServerUpsertCommand,
    McpService,
    derive_status,
)
from durin.service.principal import Principal
from durin.service.types import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationFailedError,
)

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
    """Mirrors the bits of ``McpRuntime``/``AgentLoop`` the service touches.

    ``approved`` models ``AgentLoop._mcp_servers``: seeded from a boot
    snapshot (like ``AgentLoop.__init__``'s own ``mcp_servers`` param), and
    kept current by ``mark_approved`` (``McpService.add``/``update``/
    ``enable``) and by ``connect`` itself — exactly the two write paths the
    real ``McpRuntime.approved_config`` is backed by.
    """

    def __init__(self, status: dict | None = None, errors: dict | None = None,
                 boot_snapshot: dict | None = None) -> None:
        self._status = status or {}
        self._errors = errors or {}
        self.connected: list[tuple] = []
        self.disconnected: list[str] = []
        self._approved: dict = dict(boot_snapshot or {})

    def live_status(self) -> dict:
        return self._status

    def connect_errors(self) -> dict:
        return self._errors

    async def connect(self, name: str, cfg=None) -> None:
        self.connected.append((name, cfg))
        if cfg is not None:
            self._approved[name] = cfg

    async def disconnect(self, name: str) -> None:
        self.disconnected.append(name)

    def mark_approved(self, name: str, cfg) -> None:
        self._approved[name] = cfg

    def approved_config(self, name: str):
        return self._approved.get(name)


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


def test_derive_status_failed_when_connect_errored() -> None:
    # No live connection, but the last connect attempt failed → failed (opencode
    # parity: a broken server shows failed + error, not a perpetual "connecting").
    assert derive_status(
        enabled=True,
        oauth_required=False,
        oauth_authenticated=False,
        raw=None,
        connect_error="spawn npx ENOENT",
    ) == ("failed", "spawn npx ENOENT")
    # A live connection takes precedence over a stale connect error.
    assert derive_status(
        enabled=True,
        oauth_required=False,
        oauth_authenticated=False,
        raw=_raw("closed"),
        connect_error="old error",
    ) == ("connected", None)


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


async def test_list_shows_failed_for_connect_error(config_path) -> None:
    _seed({"bad": MCPServerConfig(command="nope", enabled=True)})
    runtime = _FakeRuntime(status={}, errors={"bad": "spawn nope ENOENT"})
    res = await McpService(mcp_runtime=runtime).list(McpListQuery(), LOCAL)
    bad = next(s for s in res.servers if s.name == "bad")
    assert bad.status == "failed"
    assert bad.error == "spawn nope ENOENT"


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


# --- add / update / remove ------------------------------------------------


def _stored() -> dict:
    from durin.config.loader import load_config

    return load_config().tools.mcp_servers


async def test_add_persists_and_connects_when_enabled(config_path) -> None:
    runtime = _FakeRuntime()
    cmd = McpServerUpsertCommand(
        name="new", config=MCPServerConfig(url="https://n/mcp", enabled=True)
    )
    detail = await McpService(mcp_runtime=runtime).add(cmd, LOCAL)

    assert "new" in _stored()
    assert runtime.connected == [("new", cmd.config)]
    assert detail.name == "new"


async def test_add_disabled_does_not_connect(config_path) -> None:
    runtime = _FakeRuntime()
    cmd = McpServerUpsertCommand(
        name="off", config=MCPServerConfig(url="https://n/mcp", enabled=False)
    )
    await McpService(mcp_runtime=runtime).add(cmd, LOCAL)
    assert runtime.connected == []


async def test_add_duplicate_is_conflict(config_path) -> None:
    _seed({"dup": MCPServerConfig(url="https://d/mcp")})
    with pytest.raises(ConflictError):
        await McpService().add(
            McpServerUpsertCommand(name="dup", config=MCPServerConfig(url="https://d/mcp")),
            LOCAL,
        )


async def test_add_without_transport_is_validation_error(config_path) -> None:
    with pytest.raises(ValidationFailedError):
        await McpService().add(
            McpServerUpsertCommand(name="empty", config=MCPServerConfig()), LOCAL
        )


async def test_update_unknown_is_not_found(config_path) -> None:
    with pytest.raises(NotFoundError):
        await McpService().update(
            McpServerUpsertCommand(name="ghost", config=MCPServerConfig(url="https://g/mcp")),
            LOCAL,
        )


async def test_update_replaces_config(config_path) -> None:
    _seed({"u": MCPServerConfig(url="https://old/mcp")})
    detail = await McpService().update(
        McpServerUpsertCommand(name="u", config=MCPServerConfig(url="https://new/mcp")),
        LOCAL,
    )
    assert _stored()["u"].url == "https://new/mcp"
    assert detail.config.url == "https://new/mcp"


async def test_remove_unknown_is_not_found(config_path) -> None:
    with pytest.raises(NotFoundError):
        await McpService().remove(McpServerNameCommand(name="ghost"), LOCAL)


async def test_remove_deletes_and_disconnects_when_live(config_path) -> None:
    _seed({"r": MCPServerConfig(url="https://r/mcp")})
    runtime = _FakeRuntime({"r": _raw("closed")})
    res = await McpService(mcp_runtime=runtime).remove(McpServerNameCommand(name="r"), LOCAL)

    assert "r" not in _stored()
    assert runtime.disconnected == ["r"]
    assert res.ok is True


async def test_write_requires_mcp_write(config_path) -> None:
    with pytest.raises(ForbiddenError):
        await McpService().add(
            McpServerUpsertCommand(name="x", config=MCPServerConfig(url="https://x/mcp")),
            Principal.remote("t", set()),
        )


# --- enable / disable -----------------------------------------------------


async def test_enable_persists_and_connects(config_path) -> None:
    _seed({"e": MCPServerConfig(url="https://e/mcp", enabled=False)})
    runtime = _FakeRuntime()
    detail = await McpService(mcp_runtime=runtime).enable(
        McpServerNameCommand(name="e"), LOCAL
    )
    assert _stored()["e"].enabled is True
    assert runtime.connected and runtime.connected[0][0] == "e"
    assert detail.enabled is True


async def test_disable_persists_and_disconnects_when_live(config_path) -> None:
    _seed({"d": MCPServerConfig(url="https://d/mcp", enabled=True)})
    runtime = _FakeRuntime({"d": _raw("closed")})
    detail = await McpService(mcp_runtime=runtime).disable(
        McpServerNameCommand(name="d"), LOCAL
    )
    assert _stored()["d"].enabled is False
    assert runtime.disconnected == ["d"]
    assert detail.status == "disabled"


async def test_enable_with_a_config_persists_and_connects_exactly_that_config(config_path) -> None:
    """An approval executor enables with the snapshot a person reviewed: that
    object is what gets persisted and connected, not a later re-read."""
    _seed({"e": MCPServerConfig(command="old", enabled=False)})
    reviewed = MCPServerConfig(command="npx", args=["-y", "@x/e"], env={"A": "1"}, enabled=True)
    runtime = _FakeRuntime()
    detail = await McpService(mcp_runtime=runtime).enable(
        McpServerNameCommand(name="e"), LOCAL, config=reviewed
    )
    stored = _stored()["e"]
    assert (stored.enabled, stored.command, stored.args, stored.env) == (
        True, "npx", ["-y", "@x/e"], {"A": "1"})
    assert runtime.connected == [("e", reviewed)]
    assert runtime.approved_config("e") is reviewed
    assert detail.enabled is True


async def test_enable_with_a_config_still_needs_the_server_configured(config_path) -> None:
    with pytest.raises(NotFoundError):
        await McpService().enable(
            McpServerNameCommand(name="ghost"), LOCAL,
            config=MCPServerConfig(command="npx", enabled=True))
    assert "ghost" not in _stored()


async def test_enable_without_runtime_persists(config_path) -> None:
    _seed({"e": MCPServerConfig(url="https://e/mcp", enabled=False)})
    detail = await McpService().enable(McpServerNameCommand(name="e"), LOCAL)
    assert _stored()["e"].enabled is True
    assert detail.enabled is True


async def test_enable_unknown_is_not_found(config_path) -> None:
    with pytest.raises(NotFoundError):
        await McpService().enable(McpServerNameCommand(name="ghost"), LOCAL)


async def test_disable_unknown_is_not_found(config_path) -> None:
    with pytest.raises(NotFoundError):
        await McpService().disable(McpServerNameCommand(name="ghost"), LOCAL)


# --- oauth logout ---------------------------------------------------------


async def test_oauth_logout_clears_tokens(config_path, monkeypatch) -> None:
    _seed({"o": MCPServerConfig(url="https://o/mcp", oauth=True)})
    forgotten: list[str] = []

    class _Store:
        def __init__(self, name, server_url=None) -> None:
            self.name = name

        def forget(self) -> bool:
            forgotten.append(self.name)
            return True

    monkeypatch.setattr("durin.agent.tools.mcp_oauth.SecretsTokenStorage", _Store)

    res = await McpService().oauth_logout(McpServerNameCommand(name="o"), LOCAL)
    assert res.ok is True
    assert forgotten == ["o"]


async def test_oauth_logout_unknown_is_not_found(config_path) -> None:
    with pytest.raises(NotFoundError):
        await McpService().oauth_logout(McpServerNameCommand(name="ghost"), LOCAL)


# --- reconnect ------------------------------------------------------------


async def test_reconnect_disconnects_then_connects(config_path) -> None:
    _seed({"r": MCPServerConfig(url="https://r/mcp", enabled=True)})
    runtime = _FakeRuntime(status={"r": _raw("closed")})
    await McpService(mcp_runtime=runtime).reconnect(McpServerNameCommand(name="r"), LOCAL)
    assert runtime.disconnected == ["r"]
    assert runtime.connected and runtime.connected[0][0] == "r"


async def test_reconnect_disabled_server_is_noop(config_path) -> None:
    _seed({"d": MCPServerConfig(url="https://d/mcp", enabled=False)})
    runtime = _FakeRuntime()
    await McpService(mcp_runtime=runtime).reconnect(McpServerNameCommand(name="d"), LOCAL)
    assert runtime.connected == []
    assert runtime.disconnected == []


async def test_reconnect_unknown_is_not_found(config_path) -> None:
    with pytest.raises(NotFoundError):
        await McpService().reconnect(McpServerNameCommand(name="ghost"), LOCAL)


async def test_reconnect_applies_a_drifted_on_disk_config(config_path) -> None:
    """The shared McpService.reconnect (dashboard/REST) is the person's own
    action and always trusts the caller — the route summary says it applies
    config changes, and it must: a dashboard PATCH followed by Reconnect (or
    even a raw on-disk edit) has to actually take effect. Gating THIS method
    would break that legitimate flow; the agent-facing gate lives in
    mcp_manage.py's own reconnect handling instead, one layer up."""
    _seed({"r": MCPServerConfig(url="https://r/mcp", enabled=True)})
    runtime = _FakeRuntime(status={"r": _raw("closed")})
    await runtime.connect("r", _stored()["r"])  # an earlier connect

    _seed({"r": MCPServerConfig(url="https://new/mcp", enabled=True)})  # drifts on disk

    await McpService(mcp_runtime=runtime).reconnect(McpServerNameCommand(name="r"), LOCAL)
    assert runtime.disconnected == ["r"]
    assert runtime.connected[-1] == ("r", _stored()["r"])  # connected with the NEW config


# --- approved-config tracking (mark_approved) ------------------------------


async def test_add_marks_the_config_approved(config_path) -> None:
    runtime = _FakeRuntime()
    cmd = McpServerUpsertCommand(name="a", config=MCPServerConfig(url="https://a/mcp"))
    await McpService(mcp_runtime=runtime).add(cmd, LOCAL)
    assert runtime.approved_config("a") == cmd.config


async def test_update_marks_the_config_approved_even_though_persist_only(config_path) -> None:
    """update() never connects, so without an explicit mark the approved
    record would go stale the moment a legitimate edit lands — exactly the
    bug that made an approved agent update followed by its own reconnect
    fail in the previous round."""
    _seed({"u": MCPServerConfig(url="https://u/mcp")})
    runtime = _FakeRuntime()
    new = MCPServerConfig(url="https://u2/mcp")
    await McpService(mcp_runtime=runtime).update(
        McpServerUpsertCommand(name="u", config=new), LOCAL)
    assert runtime.approved_config("u") == new
    assert runtime.connected == []  # persist-only: no connect attempted


async def test_enable_marks_the_config_approved(config_path) -> None:
    _seed({"e": MCPServerConfig(url="https://e/mcp", enabled=False)})
    runtime = _FakeRuntime()
    await McpService(mcp_runtime=runtime).enable(McpServerNameCommand(name="e"), LOCAL)
    assert runtime.approved_config("e") is not None
    assert runtime.approved_config("e").enabled is True


# --- oauth login ----------------------------------------------------------


class _FakeFlows:
    def __init__(self, *, raises: Exception | None = None) -> None:
        self._raises = raises
        self.started: list[str] = []

    async def start(self, name, cfg, on_success=None, redirect_base=None) -> tuple[str, str]:
        if self._raises is not None:
            raise self._raises
        self.started.append(name)
        if on_success is not None:
            await on_success()
        return ("https://auth/x?state=st", "st")


async def test_oauth_login_returns_authorization_url(config_path) -> None:
    _seed({"o": MCPServerConfig(url="https://o/mcp", oauth=True)})
    flows = _FakeFlows()
    res = await McpService(oauth_flows=flows).oauth_login(
        McpOauthLoginCommand(name="o"), LOCAL
    )
    assert res.authorization_url == "https://auth/x?state=st"
    assert res.state == "st"
    assert flows.started == ["o"]


async def test_oauth_login_non_oauth_server_is_validation_error(config_path) -> None:
    _seed({"p": MCPServerConfig(url="https://p/mcp")})
    with pytest.raises(ValidationFailedError):
        await McpService().oauth_login(McpServerNameCommand(name="p"), LOCAL)


async def test_oauth_login_unknown_is_not_found(config_path) -> None:
    with pytest.raises(NotFoundError):
        await McpService().oauth_login(McpServerNameCommand(name="ghost"), LOCAL)


async def test_oauth_login_propagates_conflict(config_path) -> None:
    _seed({"o": MCPServerConfig(url="https://o/mcp", oauth=True)})
    flows = _FakeFlows(raises=ConflictError("pending", details={"name": "o"}))
    with pytest.raises(ConflictError):
        await McpService(oauth_flows=flows).oauth_login(
            McpOauthLoginCommand(name="o"), LOCAL
        )


async def test_oauth_login_resolution_chain(config_path) -> None:
    """redirect_base: config public_url wins over browser origin; origin is the
    fallback; neither set means the loopback path (redirect_base=None)."""
    _seed({"o": MCPServerConfig(url="https://o/mcp", oauth=True)})

    def _set_public_url(url: str | None) -> None:
        from durin.config.loader import get_config_path, load_config, save_config

        cfg = load_config()
        cfg.gateway.public_url = url
        save_config(cfg, get_config_path())

    captured: dict = {}

    class _CapturingFlows:
        async def start(self, name, cfg, on_success=None, redirect_base=None):
            captured["redirect_base"] = redirect_base
            return ("https://auth/x", "st")

    svc = McpService(oauth_flows=_CapturingFlows())

    # config public_url unset + origin sent -> origin wins
    await svc.oauth_login(
        McpOauthLoginCommand(name="o", origin="http://durin:8765"), LOCAL
    )
    assert captured["redirect_base"] == "http://durin:8765"

    # config public_url set -> config wins over origin
    _set_public_url("https://durin.tail9e5f5d.ts.net")
    await svc.oauth_login(
        McpOauthLoginCommand(name="o", origin="http://durin:8765"), LOCAL
    )
    assert captured["redirect_base"] == "https://durin.tail9e5f5d.ts.net"

    # neither -> None (loopback path)
    _set_public_url(None)
    await svc.oauth_login(McpOauthLoginCommand(name="o", origin=""), LOCAL)
    assert captured["redirect_base"] is None
