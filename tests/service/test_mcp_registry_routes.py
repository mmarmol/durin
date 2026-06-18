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
    monkeypatch.setattr(
        "durin.agent.mcp_registry.build_mcp_adapters", lambda regs: [_FakeReg()]
    )
    res = await McpService().registry_search(
        McpRegistrySearchQuery(q="jira", limit=5), LOCAL
    )
    assert res.hits[0].ref == "io.x/jira"
    assert res.hits[0].registry == "official"


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
