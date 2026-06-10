"""Gap observations (new:*) and principles feed the skill-extract pass.

The extractor works from distilled gaps logged in-session, not only from raw
transcript text; new skills are born compliant with active principles; a gap
whose working name materializes as a skill is marked applied.
"""
import json

from durin.agent.skill_observations import (
    add_principle,
    log_observation,
    open_observations,
)
from durin.memory.dream_passes import (
    _resolve_gap_observations,
    _skill_extract_messages,
)


def _session(ws, key, text="USER said something"):
    sdir = ws / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / f"{key}.jsonl").write_text(
        json.dumps({"role": "user", "content": text}) + "\n", encoding="utf-8")


def _gap(ws, name="release-runbook", issue="no skill covers releases"):
    return log_observation(ws, skill=f"new:{name}", kind="gap", issue=issue,
                           improvement="steps: build wheel, pipx reinstall, verify")


def test_messages_none_when_no_sessions_and_no_gaps(tmp_path):
    assert _skill_extract_messages(tmp_path, max_sessions=3) is None


def test_gap_observations_included_in_user_content(tmp_path):
    _session(tmp_path, "s1")
    _gap(tmp_path)
    msgs = _skill_extract_messages(tmp_path, max_sessions=3)
    user = msgs[-1]["content"]
    assert "no skill covers releases" in user
    assert "release-runbook" in user


def test_gaps_alone_are_enough_to_run(tmp_path):
    _gap(tmp_path)
    msgs = _skill_extract_messages(tmp_path, max_sessions=3)
    assert msgs is not None
    assert "release-runbook" in msgs[-1]["content"]


def test_principles_included_in_system_prompt(tmp_path):
    _session(tmp_path, "s1")
    add_principle(tmp_path, "skills must name their verification command")
    msgs = _skill_extract_messages(tmp_path, max_sessions=3)
    assert "skills must name their verification command" in msgs[0]["content"]


def test_resolve_gap_marks_applied_when_skill_materializes(tmp_path):
    _gap(tmp_path, name="release-runbook")
    _gap(tmp_path, name="never-built", issue="another gap entirely")
    d = tmp_path / "skills" / "release-runbook"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: release-runbook\n---\nbody\n",
                                encoding="utf-8")
    resolved = _resolve_gap_observations(tmp_path)
    assert resolved == 1
    remaining = open_observations(tmp_path)
    assert [r["skill"] for r in remaining] == ["new:never-built"]
