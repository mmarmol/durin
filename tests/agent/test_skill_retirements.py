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
async def test_the_in_session_agent_brings_one_back_only_on_the_users_word(
        tmp_path: Path) -> None:
    """The agent may call skill_write on its own initiative; re-creating a
    retired skill is the user's decision, as keeping a rejected prose body is."""
    _skill(tmp_path, "athena-logs")
    ss.remove_skill(tmp_path, "athena-logs", reason="never worked")
    tool = SkillWriteTool(tmp_path, gate_mode="override", composition_judge=None)

    refused = await _create(tool, "athena-logs")
    assert refused.get("retired") and "override_retired" in refused.get("hint", "")
    assert "athena-logs" in retired_skills(tmp_path)

    created = json.loads(await tool.execute(
        name="athena-logs", content=_body("athena-logs"),
        rationale="the user asked for it back", override_retired=True))
    assert created.get("ok"), created
    assert "athena-logs" not in retired_skills(tmp_path)


@pytest.mark.asyncio
async def test_a_quarantined_recreation_leaves_the_retirement_in_place(tmp_path: Path) -> None:
    """The skill does not exist after a quarantine, so it is still retired."""
    _skill(tmp_path, "athena-logs")
    ss.remove_skill(tmp_path, "athena-logs")
    risky = "import os, requests\ntoken = os.environ['SECRET']\nrequests.get('https://x/y')\n"
    tool = SkillWriteTool(tmp_path, gate_mode="override", composition_judge=None)

    out = json.loads(await tool.execute(
        name="athena-logs", content=_body("athena-logs"), rationale="back",
        override_retired=True, files=[{"path": "scripts/q.py", "content": risky}]))

    assert out.get("quarantined") is True
    assert "athena-logs" in retired_skills(tmp_path)


@pytest.mark.asyncio
async def test_publishing_a_draft_under_a_retired_name_needs_the_users_word(
        tmp_path: Path) -> None:
    from durin.agent.tools.skill_publish import SkillPublishTool

    _skill(tmp_path, "athena-logs")
    ss.remove_skill(tmp_path, "athena-logs")
    draft = tmp_path / "skill-drafts" / "athena-logs"
    draft.mkdir(parents=True)
    (draft / "SKILL.md").write_text(_body("athena-logs"), encoding="utf-8")
    tool = SkillPublishTool(tmp_path)

    refused = json.loads(await tool.execute(name="athena-logs"))
    assert refused.get("retired"), refused
    published = json.loads(await tool.execute(name="athena-logs", override_retired=True))
    assert published.get("ok"), published
    assert "athena-logs" not in retired_skills(tmp_path)


@pytest.mark.asyncio
async def test_a_retired_name_is_matched_in_any_spelling(tmp_path: Path) -> None:
    _skill(tmp_path, "athena-logs")
    _skill(tmp_path, "athena-boto3-query")
    ss.remove_skill(tmp_path, "athena-logs", replaced_by="athena-boto3-query")

    assert (await _create(_dream_door(tmp_path), "athena_logs")).get("retired")
    log_observation(tmp_path, skill="new:Athena_Logs", kind="gap",
                    issue="query the logs table", improvement="add the steps")

    [rec] = open_observations(tmp_path)
    assert rec["skill"] == "athena-boto3-query"


@pytest.mark.asyncio
async def test_the_retirement_check_waits_off_the_event_loop(tmp_path: Path) -> None:
    """The first read of the retirements reads the skills history under the
    store's lock, which the dream worker may hold; the gateway keeps serving."""
    import asyncio
    import threading
    import time

    _skill(tmp_path, "seed")
    ss._store_init(tmp_path).auto_commit("skill: seed")
    held = threading.Event()

    def hold_the_lock():
        with ss._store(tmp_path).write_lock():
            held.set()
            time.sleep(0.6)

    holder = threading.Thread(target=hold_the_lock)
    holder.start()
    held.wait(5)
    beats: list[float] = []

    async def heartbeat():
        for _ in range(12):
            beats.append(time.monotonic())
            await asyncio.sleep(0.05)

    beating = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.12)
    await _create(SkillWriteTool(tmp_path, gate_mode="override", composition_judge=None), "fresh")
    await beating
    holder.join()

    assert max(b - a for a, b in zip(beats, beats[1:])) < 0.3


def test_skills_removed_before_retirements_were_recorded_count_as_retired(
        tmp_path: Path) -> None:
    """A store whose removals predate the records reads them from its history:
    removals and fuse sources, unless the name exists again."""
    import shutil

    for name in ("athena-logs", "kept", "x", "y"):
        _skill(tmp_path, name)
    store = ss._store_init(tmp_path)
    store.auto_commit("skill: seed")
    skills = tmp_path / "skills"
    shutil.rmtree(skills / "athena-logs")
    store.auto_commit("skill(athena-logs): remove")
    shutil.rmtree(skills / "kept")
    store.auto_commit("skill(kept): remove")
    _skill(tmp_path, "kept")
    store.auto_commit("skill(kept): created again")
    shutil.rmtree(skills / "x")
    shutil.rmtree(skills / "y")
    _skill(tmp_path, "z")
    store.auto_commit("skill: fuse ['x', 'y'] -> z: one procedure [dream]")

    retired = retired_skills(tmp_path)

    assert set(retired) == {"athena-logs", "x", "y"}
    assert retired["x"]["replaced_by"] == "z"
    assert (skills / ".retired.jsonl").is_file()


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
