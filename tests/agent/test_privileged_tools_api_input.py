"""A turn with input from an API token cannot approve privileged actions.

In a webui conversation a person is reachable, so each privileged tool puts
its request to that person in the chat. When the turn received input from an
API token, nobody is asked there: skill and MCP changes are filed as pending
for a person (``durin approvals``), and an exec command that needs approval is
refused with nothing filed. Each test runs the tool inside its own task (as a
turn does), with the turn marked the way the agent loop marks it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from durin.agent import approval, approval_store, pending_answers
from durin.agent import approval_kinds_skills as kinds
from durin.agent.approval_prompt import ChatHandles
from durin.agent.tools.context import RequestContext
from durin.agent.user_payloads import PENDING_APPROVAL_KEY

_SESSION = "websocket:c"
_WEBUI = RequestContext(channel="websocket", chat_id="c", session_key=_SESSION)


class _Sessions:
    """Records whether a request was ever put to the person: the in-chat
    asker saves it into the session as ``pending_approval`` while it waits."""

    def __init__(self):
        self.s = SimpleNamespace(metadata={})
        self.asked = False

    def get_or_create(self, key):
        return self.s

    def save(self, session, **kw):
        self.asked = self.asked or PENDING_APPROVAL_KEY in session.metadata


def _chat(sessions: _Sessions | None = None) -> ChatHandles:
    # Handles that would ask the person in a turn without API input. The wait
    # is short: an ask that should never happen fails fast instead of hanging.
    return ChatHandles(sessions=sessions or _Sessions(), timeout_s=0.2)


@pytest.fixture(autouse=True)
def _person_reachable():
    pending_answers.reset()
    pending_answers.set_consumer_active(True)
    kinds.register_all()
    yield
    pending_answers.reset()


async def _in_api_turn(coro_fn):
    async def _turn():
        approval.note_turn_input({"origin": "api"})
        return await coro_fn()

    return await asyncio.create_task(_turn())


def _pending(ws):
    return approval_store.list_records(ws, status="pending", include_legacy=False)


class _McpService:
    def __init__(self):
        self.calls = []

    async def add(self, cmd, principal):
        self.calls.append(("add", cmd.name))
        return SimpleNamespace(model_dump=lambda: {"name": cmd.name, "status": "connected"})

    update = add


def _mcp_tool(tmp_path, service, sessions=None, timeout_s=0.2):
    from durin.agent.tools.mcp_manage import McpManageTool

    tool = McpManageTool(service=service, install_policy="approve", workspace=str(tmp_path),
                         sessions=sessions or _Sessions(), approval_timeout_s=timeout_s)
    tool.set_context(_WEBUI)
    return tool


_MCP_ADD = {"action": "add", "name": "probe", "config": {"type": "stdio", "command": "echo"}}


@pytest.mark.asyncio
async def test_mcp_manage_add_is_filed_as_pending(tmp_path) -> None:
    service, sessions = _McpService(), _Sessions()
    tool = _mcp_tool(tmp_path, service, sessions)
    out = await _in_api_turn(lambda: tool.execute(**_MCP_ADD, confirm="true"))
    assert out["status"] == "pending"
    assert sessions.asked is False
    assert service.calls == []
    [rec] = _pending(tmp_path)
    assert rec["kind"] == "mcp_change" and rec["payload"]["action"] == "add"
    assert out["approval_id"] == rec["id"]


@pytest.mark.asyncio
async def test_mcp_manage_still_runs_for_a_person(tmp_path) -> None:
    service = _McpService()
    tool = _mcp_tool(tmp_path, service, timeout_s=5)
    run = asyncio.create_task(tool.execute(**_MCP_ADD))
    for _ in range(500):
        if pending_answers.waiting_kind(_SESSION) == "approval":
            break
        await asyncio.sleep(0.01)
    assert pending_answers.resolve(_SESSION, "approve")
    out = await run
    assert out["status"] == "applied"
    assert service.calls == [("add", "probe")]


def _manual_skill(ws, name: str) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\nmetadata:\n  durin:\n    mode: manual\n---\nstep one\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_skill_edit_is_filed_as_pending(tmp_path) -> None:
    from durin.agent.tools.skill_edit import SkillEditTool

    _manual_skill(tmp_path, "demo")
    sessions = _Sessions()
    tool = SkillEditTool(workspace=tmp_path, chat=_chat(sessions))
    tool.set_context(_WEBUI)
    out = await _in_api_turn(lambda: tool.execute(
        name="demo", old="step one", new="step two", rationale="r", confirm=True))
    assert out["status"] == "pending"
    assert sessions.asked is False
    assert "step one" in (tmp_path / "skills" / "demo" / "SKILL.md").read_text()
    [rec] = _pending(tmp_path)
    assert rec["kind"] == "skill_edit"


@pytest.mark.asyncio
async def test_skill_install_deps_is_filed_as_pending(tmp_path, monkeypatch) -> None:
    from durin.agent.tools.skill_install_deps import SkillInstallDepsTool

    monkeypatch.setattr(
        "durin.agent.skills_import.runnable_install_specs",
        lambda _d: [{"kind": "pip", "value": "requests", "command": "pip install requests",
                     "needs_privileges": False}])

    async def _boom(**_k):
        raise AssertionError("install ran without a person's approval")

    sessions = _Sessions()
    tool = SkillInstallDepsTool(workspace=tmp_path, exec_run=_boom, policy="approve",
                                chat=_chat(sessions))
    tool.set_context(_WEBUI)
    out = await _in_api_turn(lambda: tool.execute(name="demo", confirm=True))
    assert out["ran"] is False
    assert out["status"] == "pending"
    assert sessions.asked is False
    [rec] = _pending(tmp_path)
    assert rec["kind"] == "skill_deps"


@pytest.mark.asyncio
async def test_skill_import_install_is_filed_as_pending(tmp_path) -> None:
    from durin.agent.tools.skill_import import SkillImportTool

    src = tmp_path / "src" / "demo"
    (src / "scripts").mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nok\n")
    (src / "scripts" / "run.sh").write_text("echo hi\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    sessions = _Sessions()
    tool = SkillImportTool(workspace=ws, allowlist=[], install_policy="approve",
                           chat=_chat(sessions))
    tool.set_context(_WEBUI)
    await tool.execute(action="fetch", source=str(src))
    out = await _in_api_turn(lambda: tool.execute(action="install", name="demo", confirm=True))
    assert out["status"] == "pending"
    assert sessions.asked is False
    assert not (ws / "skills" / "demo").exists()
    [rec] = _pending(ws)
    assert rec["kind"] == "skill_install"


@pytest.mark.asyncio
async def test_install_policy_auto_still_pre_authorizes_a_flagged_install(tmp_path) -> None:
    # The operator's standing policy is not the turn's authority: it holds
    # under API input as it does with nobody watching.
    from durin.agent.tools.skill_import import SkillImportTool

    src = tmp_path / "src" / "demo"
    (src / "scripts").mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nok\n")
    (src / "scripts" / "run.sh").write_text("echo hi\n")
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = SkillImportTool(workspace=ws, allowlist=[], install_policy="auto", chat=_chat())
    tool.set_context(_WEBUI)
    await tool.execute(action="fetch", source=str(src))
    out = await _in_api_turn(lambda: tool.execute(action="install", name="demo"))
    assert out["ok"] is True
    assert (ws / "skills" / "demo" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_exec_needing_approval_is_refused_and_files_nothing(tmp_path) -> None:
    from durin.agent.tools.shell import ExecTool

    (tmp_path / "build").mkdir()
    sessions = _Sessions()
    tool = ExecTool(working_dir=str(tmp_path), chat=_chat(sessions))
    tool.set_context(_WEBUI)
    out = await _in_api_turn(lambda: tool.execute(
        command="rm -rf build", working_dir=str(tmp_path)))
    assert out == ExecTool()._guard_command("rm -rf build", str(tmp_path))
    assert sessions.asked is False
    assert (tmp_path / "build").is_dir()
    assert approval_store.list_records(tmp_path, include_legacy=False) == []


@pytest.mark.asyncio
async def test_the_pending_note_says_why(tmp_path) -> None:
    tool = _mcp_tool(tmp_path, _McpService())
    out = await _in_api_turn(lambda: tool.execute(**_MCP_ADD))
    assert "API token" in out["message"]
