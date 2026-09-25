"""mcp_manage: MCP server CRUD + registry install, behind the approval channel."""
import asyncio
from types import SimpleNamespace

import pytest

from durin.agent import approval_store
from durin.agent import pending_answers as pa
from durin.agent.tools.mcp_manage import McpManageTool

SK = "websocket:test"


class _Result:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self):
        return dict(self.__dict__)


class _FakeService:
    """``approved`` mirrors ``McpRuntime.approved_config``: ``add``/``update``/
    ``enable`` record it automatically, same as the real ``McpService`` does
    via ``mark_approved`` — so a test can drive a real add/update through
    ``McpManageTool`` and then check that its own later reconnect succeeds."""

    def __init__(self, approved: dict | None = None):
        self.calls = []
        self._approved = dict(approved or {})

    @staticmethod
    def _persist(name, config) -> None:
        # The real McpService.add/update persists too; a fake that skips
        # this would leave config.json holding the OLD entry, so a later
        # `load_config()` read (e.g. mcp_manage's own reconnect check) would
        # see something the fake never actually wrote.
        _seed({name: config})

    async def add(self, cmd, principal):
        self.calls.append(("add", cmd.name))
        self._persist(cmd.name, cmd.config)
        self._approved[cmd.name] = cmd.config
        return _Result(name=cmd.name, status="needs_auth" if cmd.config.url else "connected")

    async def update(self, cmd, principal):
        self.calls.append(("update", cmd.name))
        self._persist(cmd.name, cmd.config)
        self._approved[cmd.name] = cmd.config
        return _Result(name=cmd.name, status="connected")

    async def remove(self, cmd, principal):
        self.calls.append(("remove", cmd.name))
        return _Result(ok=True)

    async def enable(self, cmd, principal, *, config=None):
        self.calls.append(("enable", cmd.name))
        if config is not None:
            self._persist(cmd.name, config)
            self._approved[cmd.name] = config
        return _Result(name=cmd.name, status="connected")

    async def disable(self, cmd, principal):
        self.calls.append(("disable", cmd.name))
        return _Result(name=cmd.name, status="disabled")

    async def reconnect(self, cmd, principal):
        self.calls.append(("reconnect", cmd.name))
        return _Result(name=cmd.name, status="connected")

    def approved_config(self, name):
        return self._approved.get(name)


class _Reg:
    name = "official"

    async def describe(self, ref):
        from durin.agent.mcp_registry import parse_server_json

        return parse_server_json({
            "name": ref, "version": "1.0.0",
            "remotes": [{"type": "streamable-http", "url": "https://m/x"}],
        })


class _LocalReg:
    name = "official"

    async def describe(self, ref):
        from durin.agent.mcp_registry import parse_server_json

        return parse_server_json({
            "name": ref, "version": "1.0.0",
            "packages": [{
                "registryType": "npm", "transport": {"type": "stdio"},
                "runtimeHint": "npx", "identifier": "@x/fs", "version": "1.0.0",
            }],
        })


class _Sessions:
    def __init__(self):
        self.s = SimpleNamespace(metadata={})

    def get_or_create(self, key):
        return self.s

    def save(self, session, **kw):
        pass


@pytest.fixture(autouse=True)
def _reset_waiters():
    pa.reset()
    yield
    pa.reset()


def _interactive(tool, session_key=SK):
    """Give the tool the context the agent loop always sets before execute:
    a chat session with a live consumer — i.e. a person who can approve."""
    from durin.agent.tools.context import RequestContext

    pa.set_consumer_active(True)
    tool.set_context(RequestContext(channel="websocket", chat_id="c",
                                    session_key=session_key))
    return tool


def _tool(policy="auto", service=None, workspace=None, session_key=SK, exec_run=None):
    return _interactive(McpManageTool(
        service=service or _FakeService(), exec_run=exec_run,
        install_policy=policy, registries=[],
        workspace=str(workspace) if workspace is not None else ".",
        sessions=_Sessions(), approval_timeout_s=5), session_key)


async def _answer(verdict, session_key=SK):
    for _ in range(500):
        if pa.waiting_kind(session_key) == "approval":
            assert pa.resolve(session_key, verdict)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("mcp_manage never asked for approval")


def _seed(servers: dict) -> None:
    from durin.config.loader import get_config_path, load_config, save_config

    cfg = load_config()
    cfg.tools.mcp_servers.update(servers)
    save_config(cfg, get_config_path())


@pytest.mark.asyncio
async def test_add_explicit_auto():
    svc = _FakeService()
    out = await _tool("auto", svc).execute(
        action="add", name="local-fs",
        config={"type": "stdio", "command": "npx", "args": ["-y", "@x/fs", "/tmp"]})
    assert out["name"] == "local-fs"
    assert svc.calls[0][0] == "add"


@pytest.mark.asyncio
async def test_add_in_chat_runs_after_the_user_approves(tmp_path):
    svc = _FakeService()
    run = asyncio.create_task(_tool("approve", svc, tmp_path).execute(
        action="add", name="x", config={"type": "stdio", "command": "npx"}))
    await _answer("approve")
    out = await run
    assert out["status"] == "applied" and out["result"]["name"] == "x"
    assert svc.calls == [("add", "x")]
    [rec] = approval_store.list_records(tmp_path, include_legacy=False)
    assert rec["status"] == "applied" and rec["decided_by"]["kind"] == "user"


@pytest.mark.asyncio
async def test_add_in_chat_does_not_run_when_the_user_declines(tmp_path):
    svc = _FakeService()
    run = asyncio.create_task(_tool("approve", svc, tmp_path).execute(
        action="add", name="x", config={"type": "stdio", "command": "npx"}))
    await _answer("reject")
    out = await run
    assert out["status"] == "rejected" and "do not retry" in out["message"].lower()
    assert svc.calls == []


@pytest.mark.asyncio
async def test_model_supplied_confirm_is_ignored_in_an_autonomous_context(tmp_path):
    svc = _FakeService()
    out = await _tool("approve", svc, tmp_path, session_key="cron:nightly").execute(
        action="add", name="x", config={"type": "stdio", "command": "npx"}, confirm="true")
    assert out["status"] == "pending"
    assert svc.calls == []


@pytest.mark.asyncio
async def test_model_supplied_confirm_is_ignored_on_an_interactive_session(tmp_path):
    """A cron session is pending with or without confirm, which proves nothing
    about confirm itself. On an INTERACTIVE session (a live consumer, per
    ``_interactive``) the pre-fix code read ``confirm=True`` from the call and
    ran the change immediately, skipping the ask entirely. Here the user must
    still be asked, and a decline must record the rejection, not run it."""
    svc = _FakeService()
    run = asyncio.create_task(_tool("approve", svc, tmp_path).execute(
        action="add", name="x", config={"type": "stdio", "command": "npx"},
        confirm=True))
    await _answer("reject")
    out = await run
    assert out["status"] == "rejected"
    assert svc.calls == []
    [rec] = approval_store.list_records(tmp_path, include_legacy=False)
    assert rec["status"] == "rejected" and rec["decided_by"]["kind"] == "user"


def test_confirm_is_not_in_the_schema():
    props = McpManageTool(service=_FakeService()).parameters["properties"]
    assert "confirm" not in props
    assert {"action", "ref", "name", "config", "prefer"} <= set(props)


@pytest.mark.asyncio
async def test_enable_is_gated(tmp_path):
    from durin.config.schema import MCPServerConfig

    _seed({"x": MCPServerConfig(command="npx", enabled=False)})
    svc = _FakeService()
    out = await _tool("approve", svc, tmp_path, session_key="cron:nightly").execute(
        action="enable", name="x")
    assert out["status"] == "pending"
    assert svc.calls == []
    [rec] = approval_store.list_records(tmp_path, include_legacy=False)
    # The request carries a full config snapshot (same shape as add/update),
    # not just the bare name — so a reviewer sees everything, not only that
    # SOMETHING is being enabled.
    assert rec["payload"]["action"] == "enable" and rec["payload"]["name"] == "x"
    assert rec["payload"]["config"]["command"] == "npx"
    assert rec["payload"]["config"]["enabled"] is True


@pytest.mark.asyncio
async def test_enable_of_an_already_enabled_server_is_refused(tmp_path):
    from durin.config.schema import MCPServerConfig

    _seed({"x": MCPServerConfig(command="npx", enabled=True)})
    svc = _FakeService()
    out = await _tool("approve", svc, tmp_path, session_key="cron:nightly").execute(
        action="enable", name="x")
    assert "already enabled" in out["error"]
    assert svc.calls == []
    assert approval_store.list_records(tmp_path, include_legacy=False) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["remove", "disable"])
async def test_removing_or_reapplying_is_not_gated(tmp_path, action):
    svc = _FakeService()
    await _tool("approve", svc, tmp_path, session_key="cron:nightly").execute(
        action=action, name="x")
    assert svc.calls == [(action, "x")]
    assert approval_store.list_records(tmp_path, include_legacy=False) == []


# --- reconnect: not approval-gated, but checked against approved_config ----


@pytest.mark.asyncio
async def test_reconnect_succeeds_when_on_disk_matches_the_approved_config(tmp_path):
    from durin.config.schema import MCPServerConfig

    sc = MCPServerConfig(command="npx")
    _seed({"x": sc})
    svc = _FakeService(approved={"x": sc})
    out = await _tool("approve", svc, tmp_path, session_key="cron:nightly").execute(
        action="reconnect", name="x")
    assert svc.calls == [("reconnect", "x")]
    assert "error" not in out
    assert approval_store.list_records(tmp_path, include_legacy=False) == []


@pytest.mark.asyncio
async def test_reconnect_refuses_when_never_approved(tmp_path):
    """A server that appeared in config.json outside durin's own write paths
    (an out-of-band edit, a hand-written entry) has no approved record at
    all. Refuse — do not treat "no record" as "nothing to compare, allow
    it": that is exactly the bypass this check exists to close."""
    from durin.config.schema import MCPServerConfig

    _seed({"x": MCPServerConfig(command="evil-cmd")})
    svc = _FakeService()  # no approved record for "x"
    out = await _tool("approve", svc, tmp_path, session_key="cron:nightly").execute(
        action="reconnect", name="x")
    assert svc.calls == []
    assert "changed outside durin" in out["error"]


@pytest.mark.asyncio
async def test_reconnect_refuses_when_drifted_since_approval(tmp_path):
    from durin.config.schema import MCPServerConfig

    approved = MCPServerConfig(command="npx")
    _seed({"x": MCPServerConfig(command="evil-cmd")})  # on-disk moved after approval
    svc = _FakeService(approved={"x": approved})
    out = await _tool("approve", svc, tmp_path, session_key="cron:nightly").execute(
        action="reconnect", name="x")
    assert svc.calls == []
    assert "changed outside durin" in out["error"]


@pytest.mark.asyncio
async def test_reconnect_unknown_server(tmp_path):
    svc = _FakeService()
    out = await _tool("approve", svc, tmp_path, session_key="cron:nightly").execute(
        action="reconnect", name="ghost")
    assert svc.calls == []
    assert "no such mcp server" in out["error"].lower()


@pytest.mark.asyncio
async def test_reconnect_succeeds_after_its_own_approved_update(tmp_path):
    """The exact regression this round fixes: an install_policy=auto update
    (or an approved one) marks the config approved, so the agent's own later
    reconnect must succeed — it must not refuse just because reconnect
    itself never connected before."""
    from durin.config.schema import MCPServerConfig

    _seed({"good": MCPServerConfig(url="https://good/mcp")})
    svc = _FakeService()
    auto = _tool("auto", svc, tmp_path, session_key="cron:nightly")

    r1 = await auto.execute(action="update", name="good",
                            config={"url": "https://good/mcp", "tool_timeout": 99})
    assert "error" not in r1

    r2 = await auto.execute(action="reconnect", name="good")
    assert "error" not in r2
    assert svc.calls[-1] == ("reconnect", "good")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["add", "update", "install", "enable"])
async def test_never_refuses_every_gated_action(tmp_path, action):
    svc = _FakeService()
    out = await _tool("never", svc, tmp_path).execute(
        action=action, name="x", ref="io.x/x", config={"command": "npx"})
    assert out["refused"] == "install_policy=never"
    assert svc.calls == []


@pytest.mark.asyncio
async def test_credential_literal_is_refused_before_anything_is_filed(tmp_path):
    svc = _FakeService()
    out = await _tool("approve", svc, tmp_path, session_key="cron:nightly").execute(
        action="add", name="gh",
        config={"command": "npx", "env": {"GITHUB_TOKEN": "ghp_" + "c" * 36}})
    assert "request_secret" in out["error"]
    assert "ghp_" not in out["error"]
    assert approval_store.list_records(tmp_path, include_legacy=False) == []
    assert svc.calls == []


@pytest.mark.asyncio
async def test_pending_request_approved_later_from_the_cli_writes_config(tmp_path):
    from durin.agent import approval
    from durin.agent.approval_executors import ExecDeps
    from durin.config.loader import load_config

    out = await _tool("approve", _FakeService(), tmp_path, session_key="cron:nightly").execute(
        action="add", name="fs", config={"command": "npx", "args": ["-y", "@x/fs"]})
    assert out["status"] == "pending"
    done = await approval.decide(tmp_path, out["approval_id"], "approve",
                                 decided_by={"kind": "operator", "channel": "cli"},
                                 deps=ExecDeps())
    assert done.status == "applied"
    assert load_config().tools.mcp_servers["fs"].args == ["-y", "@x/fs"]
    assert "restarts" in done.result["note"]


@pytest.mark.asyncio
async def test_unknown_action():
    out = await _tool().execute(action="frobnicate")
    assert "error" in out


@pytest.mark.asyncio
async def test_install_remote_auto(monkeypatch):
    import durin.agent.tools.mcp_manage as m

    monkeypatch.setattr(m, "build_mcp_adapters", lambda regs: [_Reg()])
    svc = _FakeService()
    out = await _tool("auto", svc).execute(action="install", ref="io.x/jira", prefer="remote")
    assert out["name"] == "jira"
    assert out["needs_oauth"] is True
    assert svc.calls[0][0] == "add"


@pytest.mark.asyncio
async def test_install_without_a_person_files_the_resolved_config(monkeypatch, tmp_path):
    import durin.agent.mcp_install as inst
    import durin.agent.tools.mcp_manage as m

    monkeypatch.setattr(m, "build_mcp_adapters", lambda regs: [_LocalReg()])
    monkeypatch.setattr(inst, "runtime_present", lambda rt: False)
    ran: list[str] = []

    async def fake_exec(**kw):
        ran.append(kw.get("command"))
        return "ok"

    svc = _FakeService()
    out = await _tool("approve", svc, tmp_path, session_key="cron:nightly",
                      exec_run=fake_exec).execute(action="install", ref="io.x/fs",
                                                  prefer="local")
    assert out["status"] == "pending"
    assert ran == [] and svc.calls == []
    [rec] = approval_store.list_records(tmp_path, include_legacy=False)
    assert rec["payload"]["config"]["command"] == "npx"
    assert "@x/fs@1.0.0" in rec["payload"]["config"]["args"]
    assert rec["payload"]["runtime_plan"]["runtime"] == "npx"


@pytest.mark.asyncio
async def test_install_local_missing_runtime_runs_install_command(monkeypatch):
    """The auto-install path (runtime missing → brew/apt install) actually fires the
    right command through exec_run before adding the server."""
    import durin.agent.mcp_install as inst
    import durin.agent.tools.mcp_manage as m

    monkeypatch.setattr(m, "build_mcp_adapters", lambda regs: [_LocalReg()])
    monkeypatch.setattr(inst, "runtime_present", lambda rt: False)  # node "missing"
    ran: list[str] = []

    async def fake_exec(**kw):
        ran.append(kw.get("command"))
        return "ok"

    svc = _FakeService()
    tool = McpManageTool(
        service=svc, exec_run=fake_exec, install_policy="auto", registries=[])
    out = await tool.execute(action="install", ref="io.x/fs", prefer="local")
    assert any("node" in (c or "") for c in ran)  # ran the runtime install
    assert svc.calls[0][0] == "add"  # then added the server
    assert out["name"] == "fs"


@pytest.mark.asyncio
async def test_create_wires_the_non_asking_exec_runner(tmp_path):
    """A runtime-install step is part of the install being approved, not a
    fresh chat turn: it must run through ExecTool's non-asking ``_run``, so a
    command that hits the deny list fails the step with the refusal text
    instead of opening a second, nested approval mid-install.

    Asserts the wiring directly (``__func__ is ExecTool._run``): a
    context-less ``ExecTool`` never asks either way, so a behavioral-only
    check here would pass just as well with ``.execute`` wired in — it would
    not have caught a regression back to the asking entry point."""
    from durin.agent.tools.context import ToolContext
    from durin.agent.tools.shell import ExecTool
    from durin.config.schema import Config

    cfg = Config()
    ctx = ToolContext(config=cfg.tools, app_config=cfg, workspace=str(tmp_path))
    tool = McpManageTool.create(ctx)

    assert tool._exec_run.__func__ is ExecTool._run

    out = await tool._exec_run(command=f"rm -rf {tmp_path}/whatever")

    assert "blocked by deny pattern" in out
    assert approval_store.list_records(tmp_path, include_legacy=False) == []
    assert pa.waiting_kind(SK) is None


def test_mcp_manage_discoverable():
    import durin.agent.tools as tools_pkg
    from durin.agent.tools.loader import ToolLoader

    classes = ToolLoader(tools_pkg).discover()
    assert any(c.__name__ == "McpManageTool" for c in classes)
