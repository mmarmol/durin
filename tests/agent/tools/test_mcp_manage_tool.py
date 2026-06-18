"""Phase 3 / Task 12 — mcp_manage gated CRUD + install tool."""
import pytest

from durin.agent.tools.mcp_manage import McpManageTool


class _Result:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self):
        return dict(self.__dict__)


class _FakeService:
    def __init__(self):
        self.calls = []

    async def add(self, cmd, principal):
        self.calls.append(("add", cmd.name))
        return _Result(name=cmd.name, status="needs_auth" if cmd.config.url else "connected")

    async def update(self, cmd, principal):
        self.calls.append(("update", cmd.name))
        return _Result(name=cmd.name, status="connected")

    async def remove(self, cmd, principal):
        self.calls.append(("remove", cmd.name))
        return _Result(ok=True)


class _Reg:
    name = "official"

    async def describe(self, ref):
        from durin.agent.mcp_registry import parse_server_json

        return parse_server_json({
            "name": ref, "version": "1.0.0",
            "remotes": [{"type": "streamable-http", "url": "https://m/x"}],
        })


def _tool(policy="auto", service=None):
    return McpManageTool(service=service or _FakeService(), exec_run=None,
                         install_policy=policy, registries=[])


@pytest.mark.asyncio
async def test_add_explicit_auto():
    svc = _FakeService()
    out = await _tool("auto", svc).execute(
        action="add", name="local-fs",
        config={"type": "stdio", "command": "npx", "args": ["-y", "@x/fs", "/tmp"]})
    assert out["name"] == "local-fs"
    assert svc.calls[0][0] == "add"


@pytest.mark.asyncio
async def test_add_approve_dry_run_then_confirm():
    svc = _FakeService()
    dry = await _tool("approve", svc).execute(
        action="add", name="x", config={"type": "stdio", "command": "npx"})
    assert dry.get("dry_run") is True
    assert not svc.calls  # nothing mutated on dry-run
    ran = await _tool("approve", svc).execute(
        action="add", name="x", config={"type": "stdio", "command": "npx"}, confirm="true")
    assert ran.get("name") == "x"
    assert svc.calls


@pytest.mark.asyncio
async def test_remove_not_gated():
    svc = _FakeService()
    await _tool("approve", svc).execute(action="remove", name="x")
    assert svc.calls[0] == ("remove", "x")


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
async def test_install_dry_run(monkeypatch):
    import durin.agent.tools.mcp_manage as m

    monkeypatch.setattr(m, "build_mcp_adapters", lambda regs: [_Reg()])
    out = await _tool("approve", _FakeService()).execute(action="install", ref="io.x/jira")
    assert out["dry_run"] is True


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


def test_mcp_manage_discoverable():
    import durin.agent.tools as tools_pkg
    from durin.agent.tools.loader import ToolLoader

    classes = ToolLoader(tools_pkg).discover()
    assert any(c.__name__ == "McpManageTool" for c in classes)
