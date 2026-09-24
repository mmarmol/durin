"""mcp_change approvals: secret-safe payloads, a hash over the live entry, execution."""
from __future__ import annotations

import pytest

from durin.agent import approval_executors as ex
from durin.agent import approval_kinds_mcp as mk
from durin.config.schema import MCPServerConfig

GH_VALUE = "ghp_" + "a" * 36


def _seed(servers: dict) -> None:
    from durin.config.loader import get_config_path, load_config, save_config

    cfg = load_config()
    cfg.tools.mcp_servers.update(servers)
    save_config(cfg, get_config_path())


def _servers() -> dict:
    from durin.config.loader import load_config

    return load_config().tools.mcp_servers


def _record(p):
    return {"kind": p.kind, "payload": p.payload}


def test_stored_secret_value_becomes_a_reference():
    from durin.security.secrets import store_secret

    store_secret("GH_TOKEN", GH_VALUE, service="github", scope=[])
    safe = mk.secret_safe_config("gh", {"command": "npx", "env": {
        "GITHUB_TOKEN": GH_VALUE, "LOG_LEVEL": "debug"}})
    assert safe["env"] == {"GITHUB_TOKEN": "${secret:GH_TOKEN}", "LOG_LEVEL": "debug"}


def test_existing_reference_is_kept_and_a_dangling_one_refused():
    from durin.security.secrets import store_secret

    store_secret("JIRA", "tok-123456789", service="jira", scope=[])
    safe = mk.secret_safe_config("j", {"url": "https://j", "headers": {
        "Authorization": "${secret:JIRA}"}})
    assert safe["headers"]["Authorization"] == "${secret:JIRA}"
    with pytest.raises(mk.SecretValueError, match="NOPE"):
        mk.secret_safe_config("j", {"url": "https://j", "headers": {
            "Authorization": "${secret:NOPE}"}})


@pytest.mark.parametrize("section,key,value", [
    ("env", "GITHUB_PERSONAL_ACCESS_TOKEN", "not-a-stored-value-123"),
    ("headers", "Authorization", "Bearer abcdefghijklmnop"),
    ("headers", "X-Custom", "sk-" + "b" * 30),
    ("env", "FOO", "«redacted:GH_TOKEN»"),
    ("oauth", "clientSecret", "s3cr3t-value-123"),
])
def test_unstored_credential_is_refused_without_echoing_it(section, key, value):
    with pytest.raises(mk.SecretValueError) as err:
        mk.secret_safe_config("s", {"url": "https://x", section: {key: value}})
    assert "request_secret" in str(err.value)
    assert f"{section}.{key}" in str(err.value)
    assert value not in str(err.value)


def test_short_or_neutral_values_stay_literal():
    safe = mk.secret_safe_config("s", {"command": "npx", "env": {
        "USE_TOKEN_CACHE": "true", "NODE_ENV": "production"}})
    assert safe["env"] == {"USE_TOKEN_CACHE": "true", "NODE_ENV": "production"}


def test_prepare_upsert_is_secret_safe_and_shows_the_target():
    from durin.security.secrets import store_secret

    store_secret("GH_TOKEN", GH_VALUE, service="github", scope=[])
    p = mk.prepare_upsert("add", "gh", {"command": "npx", "args": ["-y", "@x/gh"],
                                        "env": {"GITHUB_TOKEN": GH_VALUE}})
    assert p.kind == "mcp_change" and p.summary == "add MCP server 'gh'"
    assert p.payload["config"]["env"] == {"GITHUB_TOKEN": "${secret:GH_TOKEN}"}
    assert GH_VALUE not in repr(p.payload) and GH_VALUE not in repr(p.detail)
    assert p.detail["server"] == "gh → npx -y @x/gh"


def test_prepare_upsert_rejects_a_server_with_no_command_or_url():
    with pytest.raises(ValueError, match="command"):
        mk.prepare_upsert("add", "x", {"env": {}})


def test_prepare_enable_needs_a_configured_server():
    with pytest.raises(ValueError, match="no MCP server"):
        mk.prepare_enable("ghost")


def test_hash_covers_the_current_server_entry():
    _seed({"x": MCPServerConfig(command="npx", enabled=False)})
    p = mk.prepare_enable("x")
    assert ex.current_hash("/ws", _record(p)) == p.change_hash
    _seed({"x": MCPServerConfig(command="uvx", enabled=False)})
    assert ex.current_hash("/ws", _record(p)) != p.change_hash


@pytest.mark.asyncio
async def test_without_a_live_handle_the_change_is_written_to_config():
    p = mk.prepare_upsert("add", "fs", {"command": "npx", "args": ["-y", "@x/fs"]})
    out = await ex.execute("/ws", _record(p), ex.ExecDeps())
    assert _servers()["fs"].command == "npx"
    assert "restarts" in out["note"]
    assert "config" not in out["result"]


@pytest.mark.asyncio
async def test_a_given_handle_is_used():
    calls = []

    class _Svc:
        async def enable(self, cmd, principal):
            calls.append(("enable", cmd.name))
            return {"name": cmd.name, "status": "connected"}

    _seed({"x": MCPServerConfig(command="npx", enabled=False)})
    p = mk.prepare_enable("x")
    out = await ex.execute("/ws", _record(p), ex.ExecDeps(mcp=_Svc()))
    assert calls == [("enable", "x")]
    assert out["result"]["status"] == "connected" and "note" not in out


class _LocalDetail:
    @staticmethod
    def make():
        from durin.agent.mcp_registry import parse_server_json

        return parse_server_json({
            "name": "io.x/fs", "version": "1.0.0",
            "packages": [{
                "registryType": "npm", "transport": {"type": "stdio"},
                "runtimeHint": "npx", "identifier": "@x/fs", "version": "1.0.0",
            }],
        })


@pytest.mark.asyncio
async def test_install_resolves_the_config_and_runs_the_runtime_plan(monkeypatch):
    import durin.agent.mcp_install as inst

    monkeypatch.setattr(inst, "runtime_present", lambda rt: False)
    p = await mk.prepare_install(_LocalDetail.make(), ref="io.x/fs", prefer="local")
    assert p.payload["name"] == "fs" and p.payload["config"]["command"] == "npx"
    assert "@x/fs@1.0.0" in p.payload["config"]["args"]
    assert p.detail["source"] == "io.x/fs"
    ran = []

    async def fake_exec(**kw):
        ran.append(kw["command"])
        return "ok"

    out = await ex.execute("/ws", _record(p), ex.ExecDeps(exec_run=fake_exec))
    assert ran and "node" in ran[0]
    assert out["runtime"] == f"ran: {ran[0]}"
    assert _servers()["fs"].args[-1] == "@x/fs@1.0.0"


@pytest.mark.asyncio
async def test_install_without_a_runner_tells_the_user_the_runtime_command(monkeypatch):
    import durin.agent.mcp_install as inst

    monkeypatch.setattr(inst, "runtime_present", lambda rt: False)
    p = await mk.prepare_install(_LocalDetail.make(), ref="io.x/fs", prefer="local")
    out = await ex.execute("/ws", _record(p), ex.ExecDeps())
    assert "is missing: run `" in out["runtime"]
    assert "fs" in _servers()


@pytest.mark.asyncio
async def test_a_blocked_runtime_install_fails_the_step_and_never_adds_the_server(monkeypatch):
    import durin.agent.mcp_install as inst

    monkeypatch.setattr(inst, "runtime_present", lambda rt: False)
    p = await mk.prepare_install(_LocalDetail.make(), ref="io.x/fs", prefer="local")

    async def blocked_exec(**kw):
        return "Error: Command blocked: brew install node is not on the allowlist"

    with pytest.raises(mk.ApprovalExecError, match="brew install node"):
        await ex.execute("/ws", _record(p), ex.ExecDeps(exec_run=blocked_exec))
    assert "fs" not in _servers()


@pytest.mark.asyncio
async def test_a_nonzero_exit_from_the_runtime_install_also_fails_the_step(monkeypatch):
    import durin.agent.mcp_install as inst

    monkeypatch.setattr(inst, "runtime_present", lambda rt: False)
    p = await mk.prepare_install(_LocalDetail.make(), ref="io.x/fs", prefer="local")

    async def failing_exec(**kw):
        return "some output\nExit code: 1"

    with pytest.raises(mk.ApprovalExecError):
        await ex.execute("/ws", _record(p), ex.ExecDeps(exec_run=failing_exec))
    assert "fs" not in _servers()
