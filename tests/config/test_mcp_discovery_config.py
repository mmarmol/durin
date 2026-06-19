"""Task 1 — mcp_discovery config + MCPServerConfig pin fields."""
from durin.config.schema import (
    McpDiscoveryConfig,
    MCPServerConfig,
    ToolsConfig,
)


def test_mcp_discovery_default_on_tools():
    disc = ToolsConfig().mcp_discovery
    assert isinstance(disc, McpDiscoveryConfig)
    assert [r.kind for r in disc.registries] == ["official"]
    assert disc.registries[0].enabled is True
    assert disc.search_limit == 10
    assert disc.install_policy == "approve"


def test_mcp_server_pin_fields_default_empty():
    sc = MCPServerConfig()
    assert sc.version == ""
    assert sc.source_ref == ""


def test_mcp_discovery_parses_from_dict():
    tools = ToolsConfig.model_validate(
        {
            "mcp_discovery": {
                "registries": [
                    {"name": "official", "kind": "official"},
                    {"name": "mpak", "kind": "mpak", "enabled": False},
                ],
                "search_limit": 25,
                "install_policy": "auto",
            }
        }
    )
    d = tools.mcp_discovery
    assert d.search_limit == 25
    assert d.install_policy == "auto"
    assert [r.enabled for r in d.registries] == [True, False]


def test_quality_filter_defaults():
    from durin.config.schema import McpDiscoveryConfig

    c = McpDiscoveryConfig()
    assert c.quality == "official"
    assert c.min_stars == 100
    assert c.github_token_secret == ""


def test_quality_accepts_all():
    from durin.config.schema import McpDiscoveryConfig

    c = McpDiscoveryConfig(quality="all", min_stars=50)
    assert c.quality == "all"
    assert c.min_stars == 50
