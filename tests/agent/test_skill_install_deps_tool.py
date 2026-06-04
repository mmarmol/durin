"""P6 — skill_install_deps: policy-aware, runs each command through an exec runner."""
import asyncio

from durin.agent.tools.skill_install_deps import SkillInstallDepsTool

_SPEC = [{"kind": "brew", "value": "gh", "command": "brew install gh",
          "needs_privileges": False}]


def _tool(tmp_path, policy, ran):
    async def _exec(command, **_):
        ran.append(command)
        return f"ran: {command}"
    return SkillInstallDepsTool(workspace=tmp_path, exec_run=_exec, policy=policy)


def test_tool_name(tmp_path):
    assert _tool(tmp_path, "approve", []).name == "skill_install_deps"


def test_dry_run_lists_without_running(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.agent.skills_import.runnable_install_specs", lambda d: _SPEC)
    ran: list = []
    out = asyncio.run(_tool(tmp_path, "approve", ran).execute(name="demo", confirm=False))
    assert out["would_run"] == ["brew install gh"]
    assert ran == [] and out["ran"] is False


def test_confirm_runs_through_exec(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.agent.skills_import.runnable_install_specs", lambda d: _SPEC)
    ran: list = []
    out = asyncio.run(_tool(tmp_path, "approve", ran).execute(name="demo", confirm=True))
    assert ran == ["brew install gh"]          # went through the exec runner
    assert out["ran"] is True
    assert out["results"][0]["command"] == "brew install gh"


def test_policy_never_never_runs(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.agent.skills_import.runnable_install_specs", lambda d: _SPEC)
    ran: list = []
    out = asyncio.run(_tool(tmp_path, "never", ran).execute(name="demo", confirm=True))
    assert ran == [] and out["ran"] is False   # 'never' ignores confirm
    assert "never" in out["note"]


def test_policy_auto_runs_without_confirm(monkeypatch, tmp_path):
    monkeypatch.setattr("durin.agent.skills_import.runnable_install_specs", lambda d: _SPEC)
    ran: list = []
    out = asyncio.run(_tool(tmp_path, "auto", ran).execute(name="demo", confirm=False))
    assert ran == ["brew install gh"] and out["ran"] is True


def test_dry_run_flags_privileges(monkeypatch, tmp_path):
    spec = [{"kind": "apt", "value": "ripgrep", "command": "apt-get install -y ripgrep",
             "needs_privileges": True}]
    monkeypatch.setattr("durin.agent.skills_import.runnable_install_specs", lambda d: spec)
    out = asyncio.run(_tool(tmp_path, "approve", []).execute(name="demo", confirm=False))
    assert out["needs_privileges"] == ["apt-get install -y ripgrep"]
