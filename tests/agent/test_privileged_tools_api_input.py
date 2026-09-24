"""A turn with input from an API token cannot approve privileged actions.

In a webui conversation a person is reachable, so the privileged tools run on
the model's ``confirm``. When the turn received input from an API token, each
tool must stage the action for a person instead — the token holder is a
program. Each test runs the tool inside its own task (as a turn does), with
the turn marked the way the agent loop marks it.
"""

from __future__ import annotations

import asyncio

import pytest

from durin.agent import approval, pending_answers
from durin.agent.tools.context import RequestContext

_WEBUI = RequestContext(channel="websocket", chat_id="c", session_key="websocket:c")


@pytest.fixture(autouse=True)
def _person_reachable(monkeypatch):
    monkeypatch.setattr(pending_answers, "_CONSUMER_ACTIVE", True)


async def _in_api_turn(coro_fn):
    async def _turn():
        approval.note_turn_input({"origin": "api"})
        return await coro_fn()

    return await asyncio.create_task(_turn())


def _mcp_tool(tmp_path):
    from durin.agent.tools.mcp_manage import McpManageTool

    class _Service:
        async def add(self, *a, **k):
            raise AssertionError("gated action executed without a person's approval")
        update = add

    tool = McpManageTool(service=_Service(), install_policy="approve", workspace=str(tmp_path))
    tool.set_context(_WEBUI)
    return tool


@pytest.mark.asyncio
async def test_mcp_manage_add_is_staged(tmp_path) -> None:
    tool = _mcp_tool(tmp_path)
    out = await _in_api_turn(lambda: tool.execute(
        action="add", name="probe", confirm="true", config='{"type":"stdio","command":"echo"}'))
    assert "staged_for_approval" in out
    assert approval.list_pending(tmp_path, "mcp")[0]["action"] == "add"


@pytest.mark.asyncio
async def test_mcp_manage_still_runs_for_a_person(tmp_path) -> None:
    tool = _mcp_tool(tmp_path)
    assert tool._gate("add", {"confirm": "true"}) == "run"


@pytest.mark.asyncio
async def test_skill_edit_is_staged(tmp_path) -> None:
    from durin.agent.tools.skill_edit import SkillEditTool

    tool = SkillEditTool(workspace=tmp_path)
    tool.set_context(_WEBUI)
    out = await _in_api_turn(lambda: tool.execute(name="demo", old="a", new="b", confirm=True))
    assert "staged_for_approval" in out


@pytest.mark.asyncio
async def test_skill_install_deps_is_staged(tmp_path, monkeypatch) -> None:
    from durin.agent.tools.skill_install_deps import SkillInstallDepsTool

    monkeypatch.setattr(
        "durin.agent.skills_import.runnable_install_specs",
        lambda _d: [{"command": "pip install requests", "needs_privileges": False}])

    async def _boom(**_k):
        raise AssertionError("install ran without a person's approval")

    tool = SkillInstallDepsTool(workspace=tmp_path, exec_run=_boom)
    tool.set_context(_WEBUI)
    out = await _in_api_turn(lambda: tool.execute(name="demo", confirm=True))
    assert out["ran"] is False
    assert "staged_for_approval" in out


@pytest.mark.asyncio
async def test_skill_import_install_is_staged(tmp_path) -> None:
    from durin.agent.tools.skill_import import SkillImportTool

    tool = SkillImportTool(workspace=tmp_path, allowlist=[])
    tool.set_context(_WEBUI)
    out = await _in_api_turn(lambda: tool.execute(action="install", name="demo", confirm=True))
    assert "staged_for_approval" in out


@pytest.mark.asyncio
async def test_the_staged_note_says_why(tmp_path) -> None:
    tool = _mcp_tool(tmp_path)
    out = await _in_api_turn(lambda: tool.execute(
        action="add", name="probe", confirm="true", config='{"type":"stdio","command":"echo"}'))
    assert "API token" in out["note"]
