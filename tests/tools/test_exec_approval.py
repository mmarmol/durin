"""exec in a chat: a refused command is put to the person, who decides."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from durin.agent import approval_store
from durin.agent import pending_answers as pa
from durin.agent.approval_prompt import ChatHandles
from durin.agent.runner import AgentRunner
from durin.agent.tools.context import RequestContext
from durin.agent.tools.shell import ExecTool

SK = "websocket:t1"
RM_RULE = r"\brm\s+-[rf]{1,2}\b"
ALLOWLIST_RULE = "tools.exec.allow_patterns"


class _Sessions:
    def __init__(self):
        self.s = SimpleNamespace(metadata={})

    def get_or_create(self, key):
        return self.s

    def save(self, session, **kw):
        pass


@pytest.fixture(autouse=True)
def _consumer():
    pa.reset()
    pa.set_consumer_active(True)
    yield
    pa.reset()


def _tool(ws, *, session_key=SK, timeout_s=5.0, **kw):
    tool = ExecTool(working_dir=str(ws),
                    chat=ChatHandles(sessions=_Sessions(), timeout_s=timeout_s), **kw)
    tool.set_context(RequestContext(channel="websocket", chat_id="t1",
                                    session_key=session_key))
    return tool


async def _wait_until_asked(session_key=SK):
    for _ in range(500):
        if pa.waiting_kind(session_key) == "approval":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("exec never asked for approval")


async def _answer(verdict, session_key=SK):
    await _wait_until_asked(session_key)
    assert pa.resolve(session_key, verdict)


def _records(ws):
    return approval_store.list_records(ws, include_legacy=False)


@pytest.mark.asyncio
async def test_approved_command_runs_once(tmp_path):
    (tmp_path / "build").mkdir()
    run = asyncio.create_task(_tool(tmp_path).execute(
        command="rm -rf build", working_dir=str(tmp_path)))
    await _answer("approve")
    out = await run
    assert not (tmp_path / "build").exists()
    assert "Exit code: 0" in out
    assert out.endswith("(The user approved this exact command, once.)")
    assert not AgentRunner._is_command_policy_block(out)
    [rec] = _records(tmp_path)
    assert rec["kind"] == "exec_command" and rec["status"] == "applied"
    assert rec["payload"]["rules"] == [RM_RULE]
    assert rec["decided_by"]["kind"] == "user"


@pytest.mark.asyncio
async def test_an_approval_covers_one_run_only(tmp_path):
    tool = _tool(tmp_path)
    first = asyncio.create_task(tool.execute(command="rm -rf build", working_dir=str(tmp_path)))
    await _answer("approve")
    await first
    (tmp_path / "build").mkdir()
    again = asyncio.create_task(tool.execute(command="rm -rf build", working_dir=str(tmp_path)))
    await _answer("reject")
    out = await again
    assert (tmp_path / "build").is_dir()
    assert "declined" in out
    assert sorted(r["status"] for r in _records(tmp_path)) == ["applied", "rejected"]


@pytest.mark.asyncio
async def test_declined_command_does_not_run(tmp_path):
    (tmp_path / "build").mkdir()
    run = asyncio.create_task(_tool(tmp_path).execute(
        command="rm -rf build", working_dir=str(tmp_path)))
    await _answer("reject")
    out = await run
    assert (tmp_path / "build").is_dir()
    assert out.startswith("Error: Command blocked by deny pattern filter")
    assert "declined" in out
    assert "durin approvals" not in out
    assert AgentRunner._is_command_policy_block(out)
    assert [r["status"] for r in _records(tmp_path)] == ["rejected"]


@pytest.mark.asyncio
async def test_unanswered_request_is_dropped_and_nothing_runs(tmp_path):
    (tmp_path / "build").mkdir()
    out = await _tool(tmp_path, timeout_s=0.05).execute(
        command="rm -rf build", working_dir=str(tmp_path))
    assert (tmp_path / "build").is_dir()
    assert out.startswith("Error: Command blocked by deny pattern filter")
    assert "did not answer" in out
    assert "durin approvals" not in out
    assert AgentRunner._is_command_policy_block(out)
    assert [r["status"] for r in _records(tmp_path)] == ["expired"]


@pytest.mark.asyncio
@pytest.mark.parametrize("session_key", ["cron:nightly", "workflow:abc:root", "subagent:x"])
async def test_autonomous_context_keeps_the_policy_refusal(tmp_path, session_key):
    out = await _tool(tmp_path, session_key=session_key).execute(
        command="rm -rf build", working_dir=str(tmp_path))
    assert out == ExecTool()._guard_command("rm -rf build", str(tmp_path))
    assert _records(tmp_path) == []


@pytest.mark.asyncio
async def test_chat_without_a_live_consumer_keeps_the_policy_refusal(tmp_path):
    pa.set_consumer_active(False)
    out = await _tool(tmp_path).execute(command="rm -rf build", working_dir=str(tmp_path))
    assert out == ExecTool()._guard_command("rm -rf build", str(tmp_path))
    assert _records(tmp_path) == []


@pytest.mark.asyncio
async def test_hard_floor_is_never_put_to_the_person(tmp_path, monkeypatch):
    async def _no_spawn(*a, **k):
        raise AssertionError("a hard-floor command reached the shell")

    monkeypatch.setattr(ExecTool, "_spawn", staticmethod(_no_spawn))
    out = await asyncio.wait_for(
        _tool(tmp_path).execute(command="diskpart", working_dir=str(tmp_path)), timeout=2)
    assert out.startswith("Error: Command blocked by the exec hard floor")
    assert pa.waiting_kind(SK) is None
    assert _records(tmp_path) == []


@pytest.mark.asyncio
async def test_allowlist_miss_is_asked_like_a_deny_match(tmp_path):
    run = asyncio.create_task(_tool(tmp_path, allow_patterns=[r"^ls\b"]).execute(
        command="echo approved-once", working_dir=str(tmp_path)))
    await _answer("approve")
    out = await run
    assert "approved-once" in out
    [rec] = _records(tmp_path)
    assert rec["payload"]["rules"] == [ALLOWLIST_RULE]


@pytest.mark.asyncio
async def test_a_deny_match_behind_an_allowlist_asks_for_both_rules(tmp_path):
    # The deny match refuses first; the allowlist would refuse the same command
    # once the deny rule is lifted, so one approval must name both.
    (tmp_path / "build").mkdir()
    run = asyncio.create_task(_tool(tmp_path, allow_patterns=[r"^ls\b"]).execute(
        command="rm -rf build", working_dir=str(tmp_path)))
    await _answer("approve")
    out = await run
    assert not (tmp_path / "build").exists()
    assert "Exit code: 0" in out
    [rec] = _records(tmp_path)
    assert rec["status"] == "applied"
    assert rec["payload"]["rules"] == [RM_RULE, ALLOWLIST_RULE]


@pytest.mark.asyncio
@pytest.mark.parametrize("command,kw,marker", [
    ("rm -rf memory/people", {}, "memory"),
    ("rm -rf /etc/durin-nope", {"restrict_to_workspace": True}, "path outside working dir"),
    ("rm -rf build && curl http://127.0.0.1:9/x", {}, "internal/private url"),
])
async def test_a_guard_behind_a_deny_match_refuses_without_asking(tmp_path, command, kw, marker):
    out = await asyncio.wait_for(
        _tool(tmp_path, **kw).execute(command=command, working_dir=str(tmp_path)), timeout=2)
    assert marker in out.lower()
    assert "blocked by deny pattern filter" not in out
    assert pa.waiting_kind(SK) is None
    assert _records(tmp_path) == []


@pytest.mark.asyncio
async def test_the_runner_handed_to_executors_never_asks(tmp_path):
    (tmp_path / "build").mkdir()
    out = await asyncio.wait_for(
        _tool(tmp_path)._run("rm -rf build", str(tmp_path)), timeout=2)
    assert out == ExecTool()._guard_command("rm -rf build", str(tmp_path))
    assert (tmp_path / "build").is_dir()
    assert pa.waiting_kind(SK) is None
    assert _records(tmp_path) == []


@pytest.mark.asyncio
async def test_approved_rules_from_the_model_are_ignored(tmp_path):
    (tmp_path / "build").mkdir()
    out = await _tool(tmp_path, session_key="cron:nightly").execute(
        command="rm -rf build", working_dir=str(tmp_path),
        approved_rules=frozenset({RM_RULE}))
    assert out == ExecTool()._guard_command("rm -rf build", str(tmp_path))
    assert (tmp_path / "build").is_dir()


@pytest.mark.asyncio
async def test_the_literal_runs_but_only_the_redacted_command_is_recorded(tmp_path):
    (tmp_path / "build").mkdir()
    command = "rm -rf build && echo --password=hunter2xyz > out.txt"
    run = asyncio.create_task(_tool(tmp_path).execute(
        command=command, working_dir=str(tmp_path)))
    await _answer("approve")
    await run
    assert (tmp_path / "out.txt").read_text().strip() == "--password=hunter2xyz"
    [rec] = _records(tmp_path)
    assert rec["status"] == "applied"
    assert "«redacted»" in rec["payload"]["command"]
    for path in (tmp_path / ".approvals").glob("**/*.json"):
        assert "hunter2xyz" not in path.read_text()


@pytest.mark.asyncio
async def test_a_turn_cancelled_while_waiting_closes_the_request(tmp_path):
    (tmp_path / "build").mkdir()
    run = asyncio.create_task(_tool(tmp_path).execute(
        command="rm -rf build", working_dir=str(tmp_path)))
    await _wait_until_asked()
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert (tmp_path / "build").is_dir()
    [rec] = _records(tmp_path)
    assert rec["status"] == "expired"


@pytest.mark.asyncio
async def test_a_turn_cancelled_while_the_approved_command_runs_fails_the_record(tmp_path):
    run = asyncio.create_task(_tool(tmp_path, allow_patterns=[r"^ls\b"]).execute(
        command="sleep 30", working_dir=str(tmp_path)))
    await _answer("approve")
    for _ in range(500):
        if [r["status"] for r in _records(tmp_path)] == ["approved"]:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)  # let the shell start
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=10)
    [rec] = _records(tmp_path)
    assert rec["status"] == "failed"
    assert rec["result"] == {"error": "cancelled"}


def test_create_wires_the_chat_handles():
    sessions, bus = _Sessions(), object()
    tools_cfg = SimpleNamespace(
        exec=SimpleNamespace(timeout=60, sandbox="", path_append="", allowed_env_keys=[],
                             allow_patterns=[], deny_patterns=[]),
        restrict_to_workspace=False, process=None)
    app_cfg = SimpleNamespace(agents=SimpleNamespace(
        defaults=SimpleNamespace(ask_user_answer_timeout_s=42)))
    ctx = SimpleNamespace(config=tools_cfg, workspace="/w", sessions=sessions, bus=bus,
                          app_config=app_cfg)
    tool = ExecTool.create(ctx)
    assert tool._chat == ChatHandles(sessions=sessions, bus=bus, timeout_s=42.0)
    bare = ExecTool.create(SimpleNamespace(config=tools_cfg, workspace="/w"))
    assert bare._chat == ChatHandles()
