"""Task 2 — MCP registry data model + protocol."""
from durin.agent.mcp_registry import (
    EnvVarSpec,
    McpServerDetail,
    McpServerHit,
    PackageSpec,
)


def test_hit_defaults():
    h = McpServerHit(
        name="io.github.acme/jira", ref="io.github.acme/jira",
        registry="official", kind="remote",
    )
    assert h.description == ""
    assert h.signals == {}


def test_detail_holds_install_metadata():
    d = McpServerDetail(
        name="x", ref="x", description="", version="1.2.3", repository="",
        packages=[
            PackageSpec(
                registry_type="npm", identifier="@a/b", version="1.2.3",
                runtime_hint="npx", transport_type="stdio",
                runtime_arguments=[], package_arguments=[],
                env=[EnvVarSpec(name="API_KEY", is_required=True, is_secret=True)],
            )
        ],
        remotes=[],
    )
    assert d.packages[0].env[0].is_secret is True
