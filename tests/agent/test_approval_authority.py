"""Authority-by-context approval gate.

A privileged action must never be authorized by a value the model wrote. The
gate reads the execution context (the runtime-generated session key) and, when
no human can be asked, records the request instead of running it.
"""
from __future__ import annotations

import json

import pytest

from durin.agent import approval


@pytest.mark.parametrize("session_key", [
    "cron:457d56d5:run:1784883600056",
    "cron_dream",
    "cron_dream:run:1",
    "reactive_dream",
    "workflow:a2378214ea1c:root",
    "dream_supervisor",
    "system:subagent:7",
    "gateway",
])
def test_autonomous_sessions_have_no_human(session_key: str) -> None:
    assert approval.human_reachable(session_key) is False


def test_unknown_session_key_is_treated_as_autonomous() -> None:
    # Fail closed: a context we don't recognise is not a person.
    assert approval.human_reachable(None) is False
    assert approval.human_reachable("") is False
    assert approval.human_reachable("something-new:42") is False


def test_chat_sessions_can_reach_a_human_when_a_consumer_is_live(monkeypatch) -> None:
    from durin.agent import pending_answers

    monkeypatch.setattr(pending_answers, "_CONSUMER_ACTIVE", True)
    assert approval.human_reachable("websocket:abc") is True
    assert approval.human_reachable("slack:C123") is True
    assert approval.human_reachable("cli:local") is True


def test_chat_session_without_a_live_consumer_has_no_human(monkeypatch) -> None:
    from durin.agent import pending_answers

    monkeypatch.setattr(pending_answers, "_CONSUMER_ACTIVE", False)
    assert approval.human_reachable("websocket:abc") is False


def test_gate_stages_in_an_autonomous_context(tmp_path) -> None:
    decision = approval.gate(
        tmp_path, "mcp",
        action="update",
        summary="update MCP server 'playwright'",
        detail={"command": "npx"},
        session_key="cron_dream",
    )
    assert decision.staged is True
    assert decision.allow is False
    assert "approval" in decision.message.lower()

    pending = approval.list_pending(tmp_path, "mcp")
    assert len(pending) == 1
    assert pending[0]["action"] == "update"
    assert pending[0]["session_key"] == "cron_dream"
    assert pending[0]["detail"] == {"command": "npx"}


def test_staged_requests_survive_on_disk(tmp_path) -> None:
    approval.gate(tmp_path, "mcp", action="add", summary="add server x",
                  detail={}, session_key="cron:nightly")
    files = list((tmp_path / ".approvals" / "mcp").glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["subsystem"] == "mcp"
    assert record["status"] == "pending"


def test_gate_never_reads_a_model_supplied_confirm(tmp_path) -> None:
    # The whole point: no argument can turn a staged decision into an allow.
    decision = approval.gate(
        tmp_path, "skills", action="import", summary="import skill",
        detail={"confirm": True, "approved": "true"}, session_key="cron_dream")
    assert decision.staged is True


def test_gate_allows_when_a_human_is_present(tmp_path, monkeypatch) -> None:
    from durin.agent import pending_answers

    monkeypatch.setattr(pending_answers, "_CONSUMER_ACTIVE", True)
    decision = approval.gate(tmp_path, "mcp", action="add", summary="add x",
                             detail={}, session_key="websocket:abc")
    assert decision.allow is True
    assert decision.staged is False
    assert approval.list_pending(tmp_path, "mcp") == []


def test_discard_pending(tmp_path) -> None:
    approval.gate(tmp_path, "mcp", action="add", summary="s", detail={},
                  session_key="cron:x")
    [record] = approval.list_pending(tmp_path, "mcp")
    assert approval.discard_pending(tmp_path, "mcp", record["id"]) is True
    assert approval.list_pending(tmp_path, "mcp") == []
    assert approval.discard_pending(tmp_path, "mcp", record["id"]) is False


def test_pending_answers_shares_the_autonomous_classification() -> None:
    # One source of truth: ask_user_question must not think a workflow or a
    # dream session can answer a question either.
    from durin.agent import pending_answers

    for key in ("workflow:x:root", "cron_dream", "reactive_dream"):
        assert pending_answers.can_block(key) is False


# --- tool wiring -------------------------------------------------------------


def _mcp_tool(tmp_path, session_key):
    from durin.agent.tools.context import RequestContext
    from durin.agent.tools.mcp_manage import McpManageTool

    class _Service:
        async def add(self, *a, **k):
            raise AssertionError("gated action executed without approval")
        update = add

    tool = McpManageTool(service=_Service(), install_policy="approve",
                         workspace=str(tmp_path))
    tool.set_context(RequestContext(channel="system", chat_id="c",
                                    session_key=session_key))
    return tool


@pytest.mark.asyncio
async def test_mcp_manage_stages_in_a_cron_context(tmp_path) -> None:
    tool = _mcp_tool(tmp_path, "cron_dream")
    out = await tool.execute(action="add", name="playwright",
                             config='{"type":"stdio","command":"npx"}')
    assert "staged_for_approval" in out
    assert approval.list_pending(tmp_path, "mcp")[0]["action"] == "add"


@pytest.mark.asyncio
async def test_mcp_manage_self_confirm_cannot_run_without_a_human(tmp_path) -> None:
    # The exact 2026-07-24 sequence: the model passes confirm=true itself.
    # With no reachable user the action must stage, never execute.
    tool = _mcp_tool(tmp_path, "workflow:abc:root")
    out = await tool.execute(action="update", name="playwright", confirm="true",
                             config='{"type":"stdio","command":"npx"}')
    assert "staged_for_approval" in out


@pytest.mark.asyncio
async def test_skill_install_deps_stages_without_a_human(tmp_path, monkeypatch) -> None:
    from durin.agent.tools.context import RequestContext
    from durin.agent.tools.skill_install_deps import SkillInstallDepsTool

    monkeypatch.setattr(
        "durin.agent.skills_import.runnable_install_specs",
        lambda _d: [{"command": "pip install requests", "needs_privileges": False}])

    async def _boom(**_k):
        raise AssertionError("install ran without approval")

    tool = SkillInstallDepsTool(workspace=tmp_path, exec_run=_boom)
    tool.set_context(RequestContext(channel="system", chat_id="c",
                                    session_key="cron:nightly"))
    out = await tool.execute(name="demo", confirm=True)
    assert out["ran"] is False
    assert "staged_for_approval" in out
    assert approval.list_pending(tmp_path, "skills")[0]["action"] == "install_deps"


@pytest.mark.asyncio
async def test_skill_edit_stages_an_applied_edit_without_a_human(tmp_path) -> None:
    from durin.agent.tools.context import RequestContext
    from durin.agent.tools.skill_edit import SkillEditTool

    tool = SkillEditTool(workspace=tmp_path)
    tool.set_context(RequestContext(channel="system", chat_id="c",
                                    session_key="reactive_dream"))
    out = await tool.execute(name="demo", old="a", new="b", confirm=True)
    assert "staged_for_approval" in out
    assert approval.list_pending(tmp_path, "skills")[0]["action"] == "edit"


def test_cli_lists_and_discards_pending(tmp_path) -> None:
    # A staged request the operator can never see is a black hole; the CLI is
    # the surface that makes it real. Driven through the REAL config loader via
    # --workspace: a hand-rolled config double would have its own attribute
    # names and would pass while the shipped command raised AttributeError.
    from typer.testing import CliRunner

    from durin.cli.commands import app

    ws = tmp_path / "ws"
    ws.mkdir()
    approval.gate(ws, "mcp", action="add", summary="add server playwright",
                  detail={}, session_key="cron:nightly")

    runner = CliRunner()
    listed = runner.invoke(app, ["approvals", "--workspace", str(ws)])
    assert listed.exit_code == 0, listed.output
    assert "add server playwright" in listed.stdout
    assert "cron:nightly" in listed.stdout

    [record] = approval.list_pending(ws, "mcp")
    dropped = runner.invoke(
        app, ["approvals", "--workspace", str(ws), "--discard", record["id"]])
    assert dropped.exit_code == 0, dropped.output
    assert approval.list_pending(ws, "mcp") == []
    empty = runner.invoke(app, ["approvals", "--workspace", str(ws)])
    assert "No pending approvals" in empty.stdout
