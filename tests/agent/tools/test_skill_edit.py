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


def test_applied_auto_edit_logs_improvement_observation(tmp_path):
    # A direct in-loop edit of an `auto` skill is a structural improvement
    # signal — it feeds the curation queue without depending on the agent
    # remembering to call skill_observe.
    from durin.agent.skill_observations import open_observations

    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", "step one\n", mode="auto")
    tool = SkillEditTool(workspace=ws)
    asyncio.run(tool.execute(name="mine", old="step one", new="step two",
                             rationale="clarify the first step"))
    obs = open_observations(ws, skill="mine")
    assert len(obs) == 1
    assert obs[0]["kind"] == "improvement"
    assert "clarify the first step" in obs[0]["improvement"]


def test_proposed_manual_edit_logs_no_observation(tmp_path):
    # A manual skill only returns a proposed diff (not applied) → nothing to
    # observe, and curation never reviews manual skills anyway.
    from durin.agent.skill_observations import open_observations

    ws = tmp_path / "ws"
    ws.mkdir()
    _user_skill(ws, "mine", "step one\n", mode="manual")
    tool = SkillEditTool(workspace=ws)
    asyncio.run(tool.execute(name="mine", old="step one", new="x", rationale="r"))
    assert open_observations(ws, skill="mine") == []
