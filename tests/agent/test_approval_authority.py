"""Authority by context for privileged actions.

A privileged action must never be authorized by a value the model wrote. The
runtime reads the execution context (the runtime-generated session key) and,
when no human can be asked, records the request instead of running it.
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
async def test_mcp_manage_files_a_pending_request_in_a_cron_context(tmp_path) -> None:
    from durin.agent import approval_store

    tool = _mcp_tool(tmp_path, "cron_dream")
    out = await tool.execute(action="add", name="playwright",
                             config='{"type":"stdio","command":"npx"}')
    assert out["status"] == "pending"
    [rec] = approval_store.list_records(tmp_path, include_legacy=False)
    assert rec["kind"] == "mcp_change" and rec["payload"]["action"] == "add"


@pytest.mark.asyncio
async def test_mcp_manage_self_confirm_cannot_run_without_a_human(tmp_path) -> None:
    # The exact 2026-07-24 sequence: the model passes confirm=true itself.
    # With no reachable user the action must wait for approval, never execute.
    tool = _mcp_tool(tmp_path, "workflow:abc:root")
    out = await tool.execute(action="update", name="playwright", confirm="true",
                             config='{"type":"stdio","command":"npx"}')
    assert out["status"] == "pending"


@pytest.mark.asyncio
async def test_skill_install_deps_files_a_request_without_a_human(tmp_path, monkeypatch) -> None:
    from durin.agent import approval_store
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
    out = await tool.execute(name="demo")
    assert out["ran"] is False and out["status"] == "pending"
    [rec] = approval_store.list_records(tmp_path, status="pending", include_legacy=False)
    assert rec["kind"] == "skill_deps"


@pytest.mark.asyncio
async def test_skill_edit_files_a_request_without_a_human(tmp_path) -> None:
    from durin.agent import approval_store
    from durin.agent.tools.context import RequestContext
    from durin.agent.tools.skill_edit import SkillEditTool

    d = tmp_path / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: demo\ndescription: d\n---\nstep one\n")
    tool = SkillEditTool(workspace=tmp_path)
    tool.set_context(RequestContext(channel="system", chat_id="c",
                                    session_key="reactive_dream"))
    out = await tool.execute(name="demo", old="step one", new="step two", rationale="r")
    assert out["status"] == "pending"
    assert (d / "SKILL.md").read_text().endswith("step one\n")
    [rec] = approval_store.list_records(tmp_path, status="pending", include_legacy=False)
    assert rec["kind"] == "skill_edit"


def test_cli_lists_and_discards_pending(tmp_path) -> None:
    # A pending request the operator can never see is a black hole; the CLI is
    # the surface that makes it real. Driven through the REAL config loader via
    # --workspace: a hand-rolled config double would have its own attribute
    # names and would pass while the shipped command raised AttributeError.
    # The record is written in the earlier per-subsystem layout
    # (.approvals/<subsystem>/<id>.json); the store lists and discards it as a
    # legacy row.
    from typer.testing import CliRunner

    from durin.cli.commands import app

    ws = tmp_path / "ws"
    legacy_dir = ws / ".approvals" / "mcp"
    legacy_dir.mkdir(parents=True)
    legacy = legacy_dir / "0123456789ab.json"
    legacy.write_text(json.dumps({
        "id": "0123456789ab", "subsystem": "mcp", "action": "add",
        "summary": "add server playwright", "detail": {},
        "session_key": "cron:nightly",
        "requested_at": "2026-07-24T00:00:00+00:00", "status": "pending",
    }), encoding="utf-8")

    runner = CliRunner()
    listed = runner.invoke(app, ["approvals", "--workspace", str(ws)])
    assert listed.exit_code == 0, listed.output
    assert "add server playwright" in listed.stdout
    assert "legacy:mcp" in listed.stdout

    dropped = runner.invoke(
        app, ["approvals", "--workspace", str(ws), "discard", "0123456789ab"])
    assert dropped.exit_code == 0, dropped.output
    assert not legacy.exists()
    empty = runner.invoke(app, ["approvals", "--workspace", str(ws)])
    assert "No pending approvals" in empty.stdout
