"""skill_install_deps: policy-aware; with policy 'approve' the person approves the
exact commands (in the chat, or later from Pending); commands always run through
the exec runner."""
import asyncio
from types import SimpleNamespace

import pytest

from durin.agent import approval_kinds_skills as kinds
from durin.agent import approval_store
from durin.agent import pending_answers as pa
from durin.agent import skills_import as si
from durin.agent.tools.context import RequestContext, ToolContext
from durin.agent.tools.shell import ExecTool
from durin.agent.tools.skill_install_deps import _PARAMETERS, SkillInstallDepsTool
from durin.config.schema import Config

CHAT = "websocket:test"
_SPEC = [{"kind": "brew", "value": "gh", "command": "brew install gh",
          "needs_privileges": False}]


class _Sessions:
    def __init__(self):
        self.s = SimpleNamespace(metadata={})

    def get_or_create(self, key):
        return self.s

    def save(self, session, **kw):
        pass


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    pa.reset()
    kinds.register_all()
    monkeypatch.setattr("durin.agent.skills_import.runnable_install_specs", lambda d: _SPEC)
    yield
    pa.reset()


def _tool(tmp_path, policy, ran, session=CHAT):
    async def _exec(command, **_):
        ran.append(command)
        return f"ran: {command}"
    if session.startswith("websocket:"):
        pa.set_consumer_active(True)
    tool = SkillInstallDepsTool(workspace=tmp_path, exec_run=_exec, policy=policy,
                                chat=kinds.ChatHandles(sessions=_Sessions(), timeout_s=5))
    tool.set_context(RequestContext(channel=session.split(":")[0], chat_id="c",
                                    session_key=session))
    return tool


async def _answer(key: str, verdict: str) -> None:
    for _ in range(500):
        if pa.waiting_kind(key) == "approval":
            pa.resolve(key, verdict)
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the tool never asked")


def _run_answering(tool, verdict):
    async def go():
        task = asyncio.create_task(tool.execute(name="demo"))
        await _answer(CHAT, verdict)
        return await task
    return asyncio.run(go())


def test_tool_name(tmp_path):
    assert _tool(tmp_path, "approve", []).name == "skill_install_deps"


def test_schema_has_no_confirm():
    assert "confirm" not in _PARAMETERS["properties"]


def test_approve_policy_asks_and_runs_on_yes(tmp_path):
    ran: list = []
    out = _run_answering(_tool(tmp_path, "approve", ran), "approve")
    assert ran == ["brew install gh"]          # went through the exec runner
    assert out["ran"] is True and out["status"] == "applied"
    assert out["results"][0]["command"] == "brew install gh"


def test_a_declined_install_runs_nothing(tmp_path):
    ran: list = []
    out = _run_answering(_tool(tmp_path, "approve", ran), "reject")
    assert ran == [] and out["ran"] is False and out["status"] == "rejected"


def test_policy_never_never_runs(tmp_path):
    ran: list = []
    out = asyncio.run(_tool(tmp_path, "never", ran).execute(name="demo"))
    assert ran == [] and out["ran"] is False
    assert "never" in out["note"]


def test_policy_auto_runs_without_asking(tmp_path):
    ran: list = []
    out = asyncio.run(_tool(tmp_path, "auto", ran, session="cron:x").execute(name="demo"))
    assert ran == ["brew install gh"] and out["ran"] is True


def test_privileged_commands_are_flagged_and_filed_when_nobody_can_answer(tmp_path, monkeypatch):
    spec = [{"kind": "apt", "value": "ripgrep", "command": "apt-get install -y ripgrep",
             "needs_privileges": True}]
    monkeypatch.setattr("durin.agent.skills_import.runnable_install_specs", lambda d: spec)
    ran: list = []
    out = asyncio.run(_tool(tmp_path, "approve", ran, session="cron:x").execute(name="demo"))
    assert out["needs_privileges"] == ["apt-get install -y ripgrep"]
    assert out["status"] == "pending" and ran == []
    [rec] = approval_store.list_records(tmp_path, status="pending", include_legacy=False)
    assert rec["kind"] == "skill_deps"
    assert rec["detail"]["needs_privileges"] == ["apt-get install -y ripgrep"]


def test_a_refused_step_in_an_approved_install_fails_without_a_second_approval(
        tmp_path, monkeypatch):
    # The runner comes from ExecTool.create(ctx). Even when that exec tool
    # knows this chat, a step the exec policy refuses must fail with the
    # refusal text, not put a second approval to the person mid-install.
    spec = [{"kind": "brew", "value": "x", "command": "rm -rf build",
             "needs_privileges": False}]
    monkeypatch.setattr("durin.agent.skills_import.runnable_install_specs", lambda d: spec)
    steps: list = []
    real_run_install_specs = si.run_install_specs

    async def _record_steps(specs, *, exec_run):
        steps.extend(await real_run_install_specs(specs, exec_run=exec_run))
        return steps

    monkeypatch.setattr("durin.agent.skills_import.run_install_specs", _record_steps)
    (tmp_path / "build").mkdir()
    pa.set_consumer_active(True)
    cfg = Config()
    cfg.agents.defaults.ask_user_answer_timeout_s = 1
    tool = SkillInstallDepsTool.create(ToolContext(
        config=cfg.tools, workspace=str(tmp_path), sessions=_Sessions(), app_config=cfg))
    chat = RequestContext(channel="websocket", chat_id="c", session_key=CHAT)
    tool.set_context(chat)
    tool._exec_run.__self__.set_context(chat)

    out = _run_answering(tool, "approve")

    [step] = steps
    assert step["success"] is False
    assert step["output"] == ExecTool()._guard_command("rm -rf build", str(tmp_path))
    assert (tmp_path / "build").is_dir()
    assert out["status"] == "failed" and "rm -rf build" in out["message"]
    assert [(r["kind"], r["status"]) for r in
            approval_store.list_records(tmp_path, include_legacy=False)] \
        == [("skill_deps", "failed")]
