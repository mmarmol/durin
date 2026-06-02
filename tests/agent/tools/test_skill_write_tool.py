import asyncio

from durin.agent.skills_store import read_mode
from durin.agent.tools.skill_write import _PARAMETERS, SkillWriteTool


def test_schema_requires_core_params():
    props = _PARAMETERS["properties"]
    for p in ("name", "content", "rationale"):
        assert p in props
    assert set(_PARAMETERS["required"]) >= {"name", "content", "rationale"}


def test_skill_write_tool_creates_via_skills_store(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = SkillWriteTool(workspace=ws)
    out = asyncio.run(
        tool.execute(
            name="git-helper",
            content="# Git helper\n\nuse rebase\n",
            rationale="recurring git flow",
        )
    )
    assert "git-helper" in out
    assert (ws / "skills" / "git-helper" / "SKILL.md").exists()
    assert read_mode(ws, "git-helper") == "auto"


def test_skill_write_tool_reports_errors(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    tool = SkillWriteTool(workspace=ws)
    out = asyncio.run(tool.execute(name="../bad", content="x", rationale="r"))
    assert "error" in out.lower()
