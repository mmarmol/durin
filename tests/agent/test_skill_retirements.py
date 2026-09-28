"""A skill someone retired is not re-created by the dream, and new gaps for it
land on its replacement."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from durin.agent import skills_store as ss
from durin.agent.skill_observations import log_observation, open_observations
from durin.agent.skill_retirements import retired_skills
from durin.agent.tools.skill_write import SkillWriteTool


def _skill(ws: Path, name: str) -> None:
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: the {name} procedure\n"
        f"metadata:\n  durin:\n    mode: auto\n---\n# {name}\n\nDo the steps.\n",
        encoding="utf-8",
    )


def _body(name: str) -> str:
    return f"---\nname: {name}\ndescription: does {name}\n---\n# {name}\n\nSteps.\n"


async def _create(tool: SkillWriteTool, name: str) -> dict:
    return json.loads(await tool.execute(name=name, content=_body(name), rationale="from a gap"))


def _dream_door(ws: Path) -> SkillWriteTool:
    return SkillWriteTool(ws, gate_mode="hard", composition_judge=None)


@pytest.mark.asyncio
async def test_the_dream_does_not_recreate_a_removed_skill(tmp_path: Path) -> None:
    _skill(tmp_path, "athena-logs")
    ss.remove_skill(tmp_path, "athena-logs", reason="never worked",
                    replaced_by="athena-boto3-query")

    result = await _create(_dream_door(tmp_path), "athena-logs")

    assert "error" in result
    assert "never worked" in result["error"]
    assert "athena-boto3-query" in result["error"]
    assert not (tmp_path / "skills" / "athena-logs").exists()


@pytest.mark.asyncio
async def test_a_person_can_still_create_it_in_session(tmp_path: Path) -> None:
    _skill(tmp_path, "athena-logs")
    ss.remove_skill(tmp_path, "athena-logs")

    result = await _create(SkillWriteTool(tmp_path, gate_mode="override", composition_judge=None),
                           "athena-logs")

    assert result.get("ok"), result
    assert "athena-logs" not in retired_skills(tmp_path)


@pytest.mark.asyncio
async def test_a_fused_source_points_the_dream_at_its_target(tmp_path: Path) -> None:
    _skill(tmp_path, "a")
    _skill(tmp_path, "b")
    fused = ss.dream_fuse_skills(tmp_path, target="c", content=_body("c"),
                                 sources=["a", "b"], rationale="merge")
    assert fused.get("ok"), fused

    result = await _create(_dream_door(tmp_path), "a")

    assert "error" in result
    assert "`c`" in result["error"]


def test_a_gap_for_a_retired_skill_lands_on_its_replacement(tmp_path: Path) -> None:
    _skill(tmp_path, "athena-logs")
    _skill(tmp_path, "athena-boto3-query")
    ss.remove_skill(tmp_path, "athena-logs", reason="never worked",
                    replaced_by="athena-boto3-query")

    res = log_observation(tmp_path, skill="new:athena-logs", kind="gap",
                          issue="no skill runs Athena queries", improvement="create athena-logs")

    assert res.get("ok"), res
    [rec] = open_observations(tmp_path)
    assert rec["skill"] == "athena-boto3-query"
    assert rec["kind"] == "improvement"
    assert "athena-logs" in rec["issue"]


def test_an_open_gap_for_a_retired_name_is_declined(tmp_path: Path) -> None:
    from durin.memory.dream_passes import _resolve_gap_observations

    _skill(tmp_path, "ghost")
    ss.remove_skill(tmp_path, "ghost", reason="obsolete")
    log_observation(tmp_path, skill="new:ghost", kind="gap", issue="i", improvement="m")

    _resolve_gap_observations(tmp_path)

    assert open_observations(tmp_path) == []


def test_the_extractor_is_told_which_skills_are_retired(tmp_path: Path) -> None:
    from durin.memory.dream_passes import _skill_extract_messages

    _skill(tmp_path, "athena-logs")
    ss.remove_skill(tmp_path, "athena-logs", reason="never worked",
                    replaced_by="athena-boto3-query")
    log_observation(tmp_path, skill="new:reporting", kind="gap", issue="i", improvement="m")

    messages = _skill_extract_messages(tmp_path, max_sessions=3)

    system = messages[0]["content"]
    assert "RETIRED SKILLS" in system
    assert "athena-logs" in system and "athena-boto3-query" in system
