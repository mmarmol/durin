"""Phase 2 / Task 7 — MCP registry search/describe service routes."""
from __future__ import annotations

import pytest

from durin.service.mcp import (
    McpRegistryDescribeQuery,
    McpRegistrySearchQuery,
    McpService,
)
from durin.service.principal import Principal

LOCAL = Principal.local()


@pytest.fixture()
def config_path(tmp_path, monkeypatch):
    from durin.config.loader import save_config
    from durin.config.schema import Config

    path = tmp_path / "config.json"
    save_config(Config(), path)
    monkeypatch.setattr("durin.config.loader._current_config_path", path)
    return path


class _FakeReg:
    name = "official"

    async def fetch_page(self, *, cursor=None, updated_since=None):
        return [{"name": "io.x/jira", "description": "Jira issues"}], None

    async def search(self, query, *, limit):
        from durin.agent.mcp_registry import _hit_from_server

        servers, _ = await self.fetch_page()
        return [_hit_from_server(s, registry="official") for s in servers][:limit]

    async def describe(self, ref):
        from durin.agent.mcp_registry import parse_server_json

        return parse_server_json({
            "name": ref, "version": "1.0.0",
            "packages": [{
                "registryType": "npm",
                "transport": {"type": "stdio"}, "runtimeHint": "npx",
                "identifier": "@x/jira", "version": "1.0.0",
                "environmentVariables": [
                    {"name": "JIRA_TOKEN", "isSecret": True, "isRequired": True},
                ],
            }],
            "remotes": [{"type": "streamable-http", "url": "https://m/jira"}],
        })


@pytest.mark.asyncio
async def test_registry_search_route(config_path, monkeypatch):
    from durin.agent import mcp_catalog_store

    monkeypatch.setattr(mcp_catalog_store, "load_servers", lambda: [
        {"name": "io.x/jira", "ref": "io.x/jira",
         "description": "Jira issues", "stars": 5000},
    ])
    res = await McpService().registry_search(
        McpRegistrySearchQuery(q="jira", limit=5), LOCAL
    )
    assert res.hits[0].ref == "io.x/jira"
    assert res.hits[0].registry == "official"


@pytest.mark.asyncio
async def test_registry_search_tiers_hits_and_more(config_path, monkeypatch):
    """Curated/popular land in ``hits``; below-the-floor matches land in ``more`` (the
    progressive "less popular" reveal) — one call, no 'show all' mode."""
    from durin.agent import mcp_catalog_store

    monkeypatch.setattr(mcp_catalog_store, "load_servers", lambda: [
        {"name": "io.x/jira", "ref": "io.x/jira",
         "description": "Jira issues", "stars": 5000},  # popular → hits
        {"name": "community/jira-alt", "ref": "community/jira-alt",
         "description": "Jira alt community", "stars": 5},  # below floor → more
    ])

    res = await McpService().registry_search(
        McpRegistrySearchQuery(q="jira", limit=10), LOCAL
    )
    hits_refs = {h.ref for h in res.hits}
    more_refs = {h.ref for h in res.more}
    assert "io.x/jira" in hits_refs
    assert "community/jira-alt" not in hits_refs
    assert "community/jira-alt" in more_refs


@pytest.mark.asyncio
async def test_registry_describe_route(config_path, monkeypatch):
    monkeypatch.setattr(
        "durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [_FakeReg()]
    )
    res = await McpService().registry_describe(
        McpRegistryDescribeQuery(ref="io.x/jira"), LOCAL
    )
    assert res.version == "1.0.0"
    assert res.packages[0].runtime_hint == "npx"
    assert res.packages[0].env[0].is_secret is True


@pytest.mark.asyncio
async def test_registry_install_remote(config_path, monkeypatch):
    monkeypatch.setattr(
        "durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [_FakeReg()]
    )
    from durin.service.mcp import McpRegistryInstallCommand

    res = await McpService().registry_install(
        McpRegistryInstallCommand(ref="io.x/jira", prefer="remote"), LOCAL
    )
    assert res.name == "jira"
    assert res.config.type == "streamableHttp"
    assert res.config.url == "https://m/jira"
    assert res.config.source_ref == "io.x/jira"


@pytest.mark.asyncio
async def test_registry_install_local_stores_secret(config_path, monkeypatch):
    import durin.security.secrets as s

    monkeypatch.setattr(s, "_STORE", None)
    monkeypatch.setattr(
        "durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [_FakeReg()]
    )
    from durin.service.mcp import McpRegistryInstallCommand

    res = await McpService().registry_install(
        McpRegistryInstallCommand(
            ref="io.x/jira", prefer="local", env_values={"JIRA_TOKEN": "tok-secret-12345"}
        ),
        LOCAL,
    )
    assert res.config.type == "stdio"
    # the secret is stored as a reference, never inline plaintext
    assert res.config.env["JIRA_TOKEN"].startswith("${secret:")


class _FakeOciReg:
    """github-shaped OCI server: secret declared in a `-e NAME={token}` runtime arg."""

    name = "official"

    async def describe(self, ref):
        from durin.agent.mcp_registry import parse_server_json

        return parse_server_json({
            "name": ref, "version": "1.4.0",
            "packages": [{
                "registryType": "oci",
                "identifier": "ghcr.io/github/github-mcp-server:1.4.0",
                "transport": {"type": "stdio"},
                "runtimeArguments": [{
                    "type": "named", "name": "-e",
                    "value": "GITHUB_PERSONAL_ACCESS_TOKEN={token}",
                    "isRequired": True,
                    "variables": {"token": {"isRequired": True, "isSecret": True}},
                }],
            }],
            "remotes": [{"type": "streamable-http", "url": "https://api.githubcopilot.com/mcp/"}],
        })


@pytest.mark.asyncio
async def test_registry_install_oci_docker_no_422(config_path, monkeypatch):
    """Regression: installing github's local (OCI) package used to 422 (empty command).
    Now it builds a valid `docker run … -e NAME` config with the secret stored as a ref."""
    import durin.security.secrets as s

    monkeypatch.setattr(s, "_STORE", None)
    monkeypatch.setattr(
        "durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [_FakeOciReg()]
    )
    from durin.service.mcp import McpRegistryInstallCommand

    res = await McpService().registry_install(
        McpRegistryInstallCommand(
            ref="io.github.github/github-mcp-server", prefer="local",
            env_values={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_secret_123"},
        ),
        LOCAL,
    )
    assert res.config.type == "stdio"
    assert res.config.command == "docker"
    assert res.config.env["GITHUB_PERSONAL_ACCESS_TOKEN"].startswith("${secret:")
    assert "ghcr.io/github/github-mcp-server:1.4.0" in res.config.args
    assert "-e" in res.config.args  # token forwarded into the container


@pytest.mark.asyncio
async def test_registry_runtime_docker_missing(config_path, monkeypatch):
    monkeypatch.setattr(
        "durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [_FakeOciReg()]
    )
    import durin.agent.mcp_install as mi

    monkeypatch.setattr(mi.shutil, "which", lambda _b: None)  # docker absent
    from durin.service.mcp import McpRuntimeStatusQuery

    res = await McpService().registry_runtime(
        McpRuntimeStatusQuery(ref="io.github.github/github-mcp-server", prefer="local"),
        LOCAL,
    )
    assert res.kind == "local"
    assert res.runtime == "docker"
    assert res.present is False
    assert res.auto_installable is False  # heavy runtime — user installs Docker themselves
    assert res.install_command == ""


@pytest.mark.asyncio
async def test_registry_runtime_remote_needs_nothing(config_path, monkeypatch):
    monkeypatch.setattr(
        "durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [_FakeOciReg()]
    )
    from durin.service.mcp import McpRuntimeStatusQuery

    res = await McpService().registry_runtime(
        McpRuntimeStatusQuery(ref="io.github.github/github-mcp-server", prefer="remote"),
        LOCAL,
    )
    assert res.kind == "remote"
    assert res.present is True
    assert res.runtime == ""


@pytest.mark.asyncio
async def test_registry_runtime_npx_auto_installable(config_path, monkeypatch):
    monkeypatch.setattr(
        "durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [_FakeReg()]
    )
    import durin.agent.mcp_install as mi

    monkeypatch.setattr(mi.shutil, "which", lambda _b: None)  # npx absent
    from durin.service.mcp import McpRuntimeStatusQuery

    res = await McpService().registry_runtime(
        McpRuntimeStatusQuery(ref="io.x/jira", prefer="local"), LOCAL
    )
    assert res.runtime == "npx"
    assert res.present is False
    assert res.auto_installable is True
    assert "install" in res.install_command  # a copy-paste hint (brew/apt)


def _seed_jira(version: str) -> None:
    from durin.config.loader import get_config_path, load_config, save_config
    from durin.config.schema import MCPServerConfig

    cfg = load_config()
    cfg.tools.mcp_servers["jira"] = MCPServerConfig(
        type="stdio", command="npx", args=["-y", f"@x/jira@{version}"],
        version=version, source_ref="io.x/jira")
    save_config(cfg, get_config_path())


@pytest.mark.asyncio
async def test_registry_updates_route(config_path, monkeypatch):
    monkeypatch.setattr(
        "durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [_FakeReg()]
    )
    _seed_jira("0.9.0")  # registry latest is 1.0.0 → update available
    from durin.service.mcp import McpUpdatesQuery

    res = await McpService().registry_updates(McpUpdatesQuery(), LOCAL)
    assert any(
        u.name == "jira" and u.current == "0.9.0" and u.latest == "1.0.0"
        for u in res.updates
    )


@pytest.mark.asyncio
async def test_registry_update_repins(config_path, monkeypatch):
    monkeypatch.setattr(
        "durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [_FakeReg()]
    )
    _seed_jira("0.9.0")
    from durin.config.loader import load_config
    from durin.service.mcp import McpServerNameCommand

    await McpService().registry_update(McpServerNameCommand(name="jira"), LOCAL)
    updated = load_config().tools.mcp_servers["jira"]
    assert updated.version == "1.0.0"
    assert "@x/jira@1.0.0" in updated.args


class _FakeOauthRemoteReg:
    """A hosted, OAuth-protected remote: remotes only, NO static headers (like atlassian)."""

    name = "official"

    async def describe(self, ref):
        from durin.agent.mcp_registry import parse_server_json

        return parse_server_json({
            "name": ref, "version": "1.1.1",
            "remotes": [{"type": "streamable-http", "url": "https://mcp.atlassian.com/v1/mcp"}],
        })


@pytest.mark.asyncio
async def test_registry_install_autodetects_oauth_remote(config_path, monkeypatch):
    """A header-less remote whose endpoint demands Bearer auth → install enables oauth →
    the server lands as needs_auth (sign-in), not a hang."""
    monkeypatch.setattr(
        "durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [_FakeOauthRemoteReg()]
    )
    import durin.agent.mcp_install as mi

    async def _cap(_url, *, request=None, fetch_json=None):
        return {"oauth": True, "dcr": True}  # 401-Bearer + DCR → durin can complete OAuth

    monkeypatch.setattr(mi, "remote_oauth_capability", _cap)
    from durin.service.mcp import McpRegistryInstallCommand

    res = await McpService().registry_install(
        McpRegistryInstallCommand(ref="com.atlassian/atlassian-mcp-server", prefer="remote"),
        LOCAL,
    )
    assert res.config.type == "streamableHttp"
    assert res.config.oauth is True          # oauth auto-enabled
    assert res.status == "needs_auth"        # → UI shows "sign in", no hang


@pytest.mark.asyncio
async def test_registry_install_remote_no_oauth_when_endpoint_public(config_path, monkeypatch):
    """A header-less remote that does NOT 401 (public) is left untouched — no forced oauth."""
    monkeypatch.setattr(
        "durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [_FakeOauthRemoteReg()]
    )
    import durin.agent.mcp_install as mi

    async def _no_cap(_url, *, request=None, fetch_json=None):
        return {"oauth": False, "dcr": False}

    monkeypatch.setattr(mi, "remote_oauth_capability", _no_cap)
    from durin.service.mcp import McpRegistryInstallCommand

    res = await McpService().registry_install(
        McpRegistryInstallCommand(ref="com.public/remote", prefer="remote"), LOCAL
    )
    assert not res.config.oauth


@pytest.mark.asyncio
async def test_registry_install_does_not_block_on_connect(config_path, monkeypatch):
    """The install must return immediately and settle the connection in the BACKGROUND — a
    connect that hangs (or swallows cancellation) must never freeze the install request."""
    import asyncio

    monkeypatch.setattr(
        "durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [_FakeOauthRemoteReg()]
    )
    import durin.agent.mcp_install as mi

    async def _no_cap(_url, *, request=None, fetch_json=None):
        return {"oauth": False, "dcr": False}  # plain remote → background connect (not needs_auth)

    monkeypatch.setattr(mi, "remote_oauth_capability", _no_cap)

    connected = asyncio.Event()

    class _HangRuntime:
        def live_status(self):
            return {}

        def connect_errors(self):
            return {}

        async def connect(self, name, cfg):
            connected.set()
            await asyncio.Event().wait()  # never returns

        async def disconnect(self, name):
            pass

    from durin.service.mcp import McpRegistryInstallCommand

    svc = McpService(mcp_runtime=_HangRuntime())
    # Must complete well under the timeout despite the connect hanging forever.
    res = await asyncio.wait_for(
        svc.registry_install(
            McpRegistryInstallCommand(ref="com.public/remote", prefer="remote"), LOCAL
        ),
        timeout=5,
    )
    assert res.name == "remote"          # returned without blocking on the hung connect
    await asyncio.sleep(0.05)
    assert connected.is_set()            # the connect WAS scheduled (in the background)
