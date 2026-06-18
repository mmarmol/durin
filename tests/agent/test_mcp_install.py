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
