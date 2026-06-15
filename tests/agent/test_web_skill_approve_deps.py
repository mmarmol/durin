import json
from pathlib import Path

import pytest


def _make_quarantined(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "skills").mkdir()
    (workspace / ".durin").mkdir()
    qdir = workspace / ".durin" / "import-quarantine" / "myskill"
    qdir.mkdir(parents=True)
    (qdir / "SKILL.md").write_text("---\nname: myskill\ndescription: Test skill.\n---\nHello.\n")
    (qdir / ".scan.json").write_text(json.dumps({
        "source": "github:test/repo",
        "verdict": "safe",
        "findings": [],
        "requirements": {
            "platforms": {"value": [], "inferred": False},
            "bins": [{"name": "gh", "origin": "declared", "available": None}],
            "env": [],
            "compatibility": "",
            "installable": False,
            "blocked_by_platform": False,
            "platform_conflict": False,
        },
    }))
    return workspace, qdir


@pytest.mark.asyncio
async def test_approve_with_install_deps(tmp_path, monkeypatch):
    from durin.agent import skills_store as ss

    workspace, qdir = _make_quarantined(tmp_path)
    installed_ok = {"ok": True, "name": "myskill", "verdict": "safe", "commit": "abc123"}
    monkeypatch.setattr(
        "durin.agent.skills_import.install_imported_skill",
        lambda *a, **kw: installed_ok,
    )
    monkeypatch.setattr(
        "durin.agent.skills_import.runnable_install_specs",
        lambda d: [{"kind": "brew", "value": "gh", "command": "brew install gh", "needs_privileges": False}],
    )
    calls = []

    async def mock_exec(*, command):
        calls.append(command)
        return "ok"

    monkeypatch.setattr(ss, "_get_exec_run", lambda ws: mock_exec)

    status, payload = await ss.web_skill_approve(
        workspace, "myskill", confirm=True, override=False,
        install_deps=True, exec_run=mock_exec,
    )
    assert payload["ok"] is True
    assert "deps_results" in payload
    assert len(payload["deps_results"]) == 1
    assert calls == ["brew install gh"]


@pytest.mark.asyncio
async def test_approve_without_install_deps_skips_deps(tmp_path, monkeypatch):
    from durin.agent import skills_store as ss

    workspace, qdir = _make_quarantined(tmp_path)
    installed_ok = {"ok": True, "name": "myskill", "verdict": "safe", "commit": "abc123"}
    monkeypatch.setattr(
        "durin.agent.skills_import.install_imported_skill",
        lambda *a, **kw: installed_ok,
    )

    status, payload = await ss.web_skill_approve(
        workspace, "myskill", confirm=True, override=False,
    )
    assert payload["ok"] is True
    assert "deps_results" not in payload
