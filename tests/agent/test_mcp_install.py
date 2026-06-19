"""Phase 3 / Tasks 10-11 — install orchestration + secret collection."""
import pytest

from durin.agent.mcp_install import (
    build_server_config_from_detail,
    collect_secret_env,
    has_update,
    rebuild_for_update,
    runtime_install_spec,
    runtime_present,
)
from durin.agent.mcp_registry import (
    EnvVarSpec,
    McpServerDetail,
    PackageSpec,
    RemoteSpec,
)


def _detail(**kw):
    base = dict(name="io.x/jira", ref="io.x/jira", description="", version="1.4.0",
                repository="", packages=[], remotes=[])
    base.update(kw)
    return McpServerDetail(**base)


def test_build_remote_config():
    d = _detail(remotes=[RemoteSpec(transport_type="streamable-http", url="https://m/jira")])
    sc = build_server_config_from_detail(d, prefer="remote", secret_env_refs={})
    assert sc.type == "streamableHttp"
    assert sc.url == "https://m/jira"
    assert sc.source_ref == "io.x/jira"


def test_build_local_config_pins_version_and_secret_ref():
    d = _detail(packages=[PackageSpec(
        registry_type="npm", identifier="@x/jira", version="1.4.0",
        runtime_hint="npx", transport_type="stdio",
        runtime_arguments=[], package_arguments=["--stdio"], env=[])])
    sc = build_server_config_from_detail(
        d, prefer="local", secret_env_refs={"JIRA_TOKEN": "${secret:MCP_JIRA_TOKEN}"})
    assert sc.type == "stdio"
    assert sc.command == "npx"
    assert "@x/jira@1.4.0" in sc.args  # version pinned into the launch arg
    assert sc.version == "1.4.0"
    assert sc.source_ref == "io.x/jira"
    assert sc.env["JIRA_TOKEN"] == "${secret:MCP_JIRA_TOKEN}"


def test_build_npm_config_infers_npx_when_runtime_hint_empty():
    """The registry often omits runtimeHint (e.g. microsoft/playwright-mcp is npm with an
    empty hint) — infer the runtime from registry_type so the command isn't empty (422)."""
    d = _detail(packages=[PackageSpec(
        registry_type="npm", identifier="@playwright/mcp", version="0.0.76",
        runtime_hint="", transport_type="stdio",
        runtime_arguments=[], package_arguments=[], env=[])])
    sc = build_server_config_from_detail(d, prefer="local", secret_env_refs={})
    assert sc.command == "npx"
    assert sc.args == ["-y", "@playwright/mcp@0.0.76"]


def test_build_pypi_config_infers_uvx_when_runtime_hint_empty():
    d = _detail(packages=[PackageSpec(
        registry_type="pypi", identifier="some-mcp", version="1.2.3",
        runtime_hint="", transport_type="stdio",
        runtime_arguments=[], package_arguments=[], env=[])])
    sc = build_server_config_from_detail(d, prefer="local", secret_env_refs={})
    assert sc.command == "uvx"
    assert sc.args == ["some-mcp==1.2.3"]


def test_build_oci_docker_config_forwards_secret_via_e_flag():
    """An OCI package (empty runtime_hint) launches via `docker run`, forwarding each
    env var with a passthrough `-e NAME` flag — the secret lives in env (resolved at
    spawn), never in argv."""
    d = _detail(packages=[PackageSpec(
        registry_type="oci", identifier="ghcr.io/github/github-mcp-server:1.4.0",
        version="", runtime_hint="", transport_type="stdio",
        runtime_arguments=[], package_arguments=[],
        env=[EnvVarSpec(name="GITHUB_PERSONAL_ACCESS_TOKEN",
                        is_secret=True, is_required=True)])])
    sc = build_server_config_from_detail(
        d, prefer="local",
        secret_env_refs={"GITHUB_PERSONAL_ACCESS_TOKEN": "${secret:MCP_GH_TOKEN}"})
    assert sc.type == "stdio"
    assert sc.command == "docker"
    assert sc.args == ["run", "-i", "--rm", "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
                       "ghcr.io/github/github-mcp-server:1.4.0"]
    assert sc.env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "${secret:MCP_GH_TOKEN}"
    assert all("${secret:" not in a for a in sc.args)  # secret never in argv


def test_build_docker_runtime_hint_keeps_extra_args():
    """A package with explicit runtime_hint=docker keeps non-env runtime args + image."""
    d = _detail(packages=[PackageSpec(
        registry_type="oci", identifier="x/y:2.0.0", version="",
        runtime_hint="docker", transport_type="stdio",
        runtime_arguments=["--network", "host"], package_arguments=["--stdio"],
        env=[EnvVarSpec(name="API_KEY", is_secret=True)])])
    sc = build_server_config_from_detail(
        d, prefer="local", secret_env_refs={"API_KEY": "${secret:MCP_K}"})
    assert sc.command == "docker"
    assert sc.args == ["run", "-i", "--rm", "-e", "API_KEY", "--network", "host",
                       "x/y:2.0.0", "--stdio"]


def test_local_prefer_falls_back_to_remote_when_no_packages():
    d = _detail(remotes=[RemoteSpec(transport_type="sse", url="https://m/x")])
    sc = build_server_config_from_detail(d, prefer="local", secret_env_refs={})
    assert sc.type == "sse"
    assert sc.url == "https://m/x"


def test_runtime_install_spec_known_and_unknown():
    assert runtime_install_spec("uvx") is not None
    assert runtime_install_spec("npx") is not None
    assert runtime_install_spec("docker") is None  # heavy runtime, out of scope v1


def test_runtime_present_uses_which(monkeypatch):
    import durin.agent.mcp_install as mod
    monkeypatch.setattr(mod.shutil, "which", lambda b: "/usr/bin/" + b if b == "npx" else None)
    assert runtime_present("npx") is True
    assert runtime_present("uvx") is False


@pytest.fixture()
def secret_store_tmp(tmp_path, monkeypatch):
    import durin.security.secrets as s
    from durin.config.loader import save_config
    from durin.config.schema import Config

    path = tmp_path / "config.json"
    save_config(Config(), path)
    monkeypatch.setattr("durin.config.loader._current_config_path", path)
    monkeypatch.setattr(s, "_STORE", None)
    return path


def test_collect_secret_env_stores_only_secrets(secret_store_tmp):
    d = _detail(packages=[PackageSpec(
        registry_type="npm", identifier="@x/jira", version="1.0",
        runtime_hint="npx", transport_type="stdio",
        runtime_arguments=[], package_arguments=[],
        env=[EnvVarSpec(name="JIRA_TOKEN", is_secret=True, is_required=True),
             EnvVarSpec(name="JIRA_URL", is_required=True)])])
    refs = collect_secret_env(
        d, {"JIRA_TOKEN": "abc-secret-1234", "JIRA_URL": "https://x"}, server_name="jira")
    assert refs["JIRA_TOKEN"].startswith("${secret:")
    assert "JIRA_URL" not in refs  # non-secret not stored here
    from durin.security.secrets import get_secret_store
    name = refs["JIRA_TOKEN"][len("${secret:"):-1]
    assert get_secret_store(reload=True).get(name).value == "abc-secret-1234"


def test_has_update():
    assert has_update("1.4.0", "1.5.0") is True
    assert has_update("1.4.0", "1.4.0") is False
    assert has_update("2.0.0", "1.9.9") is False  # never downgrade
    assert has_update("", "1.0.0") is False
    assert has_update("1.0.0", "") is False
    assert has_update("weird", "alsoweird") is False  # unparseable → no nag


def test_has_update_prerelease():
    # A pre-release precedes its release (semver): a user on rc/beta must be told
    # when GA ships, and a GA user must not be nagged to "upgrade" to an rc.
    assert has_update("1.0.0-rc.1", "1.0.0") is True       # GA after rc → update
    assert has_update("1.0.0-beta.5", "1.0.0") is True
    assert has_update("1.0.0", "1.0.0-rc.1") is False      # rc is older → no update
    assert has_update("1.0.0-rc.1", "1.0.0-rc.2") is True  # rc.2 newer than rc.1
    assert has_update("1.0.0+build.9", "1.0.0") is False   # build metadata != newer


def test_rebuild_for_update_repins_and_preserves_env():
    from durin.config.schema import MCPServerConfig

    old = MCPServerConfig(
        type="stdio", command="npx", args=["-y", "@x/jira@1.0.0"],
        env={"JIRA_TOKEN": "${secret:MCP_JIRA}"}, version="1.0.0",
        source_ref="io.x/jira", enabled_tools=["create_issue"])
    d = _detail(ref="io.x/jira", version="2.0.0", packages=[PackageSpec(
        registry_type="npm", identifier="@x/jira", version="2.0.0", runtime_hint="npx",
        transport_type="stdio", runtime_arguments=[], package_arguments=[], env=[])])
    new = rebuild_for_update(old, d)
    assert new.version == "2.0.0"
    assert "@x/jira@2.0.0" in new.args  # re-pinned to latest
    assert new.env == {"JIRA_TOKEN": "${secret:MCP_JIRA}"}  # secrets preserved
    assert new.enabled_tools == ["create_issue"]  # user customisation preserved


def test_rebuild_for_update_remote_is_noop():
    from durin.config.schema import MCPServerConfig

    old = MCPServerConfig(type="streamableHttp", url="https://m/x", source_ref="io.x/r")
    d = _detail(ref="io.x/r", version="9.9.9", remotes=[])
    assert rebuild_for_update(old, d) is old
