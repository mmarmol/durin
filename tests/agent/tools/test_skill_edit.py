import asyncio
from pathlib import Path

from durin.agent.tools.skill_edit import _PARAMETERS, SkillEditTool


def _user_skill(ws: Path, name: str, body: str, mode: str = "auto") -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\nmetadata:\n  durin:\n    mode: {mode}\n---\n{body}",
        encoding="utf-8",
    )


def test_schema_requires_core_params():
    props = _PARAMETERS["properties"]
    for p in ("name", "old", "new", "rationale"):
        assert p in props
    assert set(_PARAMETERS["required"]) >= {"name", "old", "new", "rationale"}


def test_execute_edits_an_auto_skill(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", "step one\n", mode="auto")
    tool = SkillEditTool(workspace=ws)
    out = asyncio.run(tool.execute(name="mine", old="step one", new="step two", rationale="clarify"))
    assert out["ok"] is True
    assert "step two" in (ws / "skills" / "mine" / "SKILL.md").read_text()


def test_execute_manual_skill_proposes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", "step one\n", mode="manual")
    tool = SkillEditTool(workspace=ws)
    out = asyncio.run(tool.execute(name="mine", old="step one", new="x", rationale="r"))
    assert out.get("proposed") is True
