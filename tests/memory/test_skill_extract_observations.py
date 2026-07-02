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
    _recent_sessions_text,
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


def test_gap_resolution_matches_normalized_names(tmp_path):
    """Gap logged as 'new:Release Runbook' should match skill 'release-runbook'
    via normalized name comparison."""
    from durin.agent.skill_observations import log_observation
    from durin.agent import skills_store as ss

    log_observation(tmp_path, skill="new:Release Runbook", kind="gap",
                    issue="no runbook", improvement="write one")
    body = "---\nname: release-runbook\ndescription: run releases\n---\n# R\n\nSteps.\n"
    assert ss.dream_create_skill(tmp_path, "release-runbook", body, "seed").get("ok")
    assert _resolve_gap_observations(tmp_path) == 1
    assert not [r for r in open_observations(tmp_path)
                if r.get("skill") == "new:Release Runbook"]


def test_recent_sessions_text_preserves_head_and_tail_when_truncating(tmp_path):
    """Long session windows keep both head (first 6000 chars) and tail (last 6000).
    This ensures late-session procedures survive truncation."""
    # Build a session with unique markers in head and tail, total > 12000 chars
    head_marker = "===HEAD_MARKER_UNIQUE_123==="
    tail_marker = "===TAIL_MARKER_UNIQUE_456==="

    # Create a long session: head section + filler + tail section
    head_text = f"{head_marker}\n" + "X" * 5000
    middle_text = "MIDDLE_" * 1000  # ~7000 chars
    tail_text = "Y" * 5500 + f"\n{tail_marker}"
    full_text = head_text + middle_text + tail_text

    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True)
    session_file = sdir / "s_long.jsonl"
    session_file.write_text(
        json.dumps({"role": "user", "content": full_text}) + "\n",
        encoding="utf-8"
    )

    result = _recent_sessions_text(tmp_path, max_sessions=1)

    # Both markers should be present
    assert head_marker in result, f"Head marker missing from result (len={len(result)})"
    assert tail_marker in result, f"Tail marker missing from result (len={len(result)})"
    # Total length should be under ~12100 (6000 + separator + 6000)
    assert len(result) <= 12100, f"Result too long: {len(result)} chars"
    # Truncation marker should be present (when > 12000 chars)
    if len(full_text) > 12000:
        assert "[... middle truncated ...]" in result
