"""Gap observations (new:*) and principles feed the skill-extract pass.

The extractor works from the logged gaps that were seen on more than one
occasion, never from raw transcript text; new skills are born compliant with
active principles; a gap whose working name materializes as a skill is marked
applied.
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


def _gap(ws, name="release-runbook", issue="no skill covers releases",
         sessions=("websocket_a", "websocket_b")):
    """A gap seen once in each of ``sessions`` — two by default, which is
    what the extractor asks for before it authors from one."""
    for session in sessions:
        out = log_observation(ws, skill=f"new:{name}", kind="gap", issue=issue,
                              improvement="steps: build wheel, pipx reinstall, verify",
                              session=session)
    return out


def test_a_session_with_no_gap_behind_it_is_nothing_to_mine(tmp_path):
    _session(tmp_path, "s1")
    assert _skill_extract_messages(tmp_path) is None


def test_gap_observations_included_in_user_content(tmp_path):
    _session(tmp_path, "s1")
    _gap(tmp_path)
    msgs = _skill_extract_messages(tmp_path)
    user = msgs[-1]["content"]
    assert "no skill covers releases" in user
    assert "release-runbook" in user


def test_gaps_alone_are_enough_to_run(tmp_path):
    _gap(tmp_path)
    msgs = _skill_extract_messages(tmp_path)
    assert msgs is not None
    assert "release-runbook" in msgs[-1]["content"]


def test_principles_included_in_system_prompt(tmp_path):
    _gap(tmp_path)
    add_principle(tmp_path, "skills must name their verification command")
    msgs = _skill_extract_messages(tmp_path)
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
    from durin.agent import skills_store as ss
    from durin.agent.skill_observations import log_observation

    log_observation(tmp_path, skill="new:Release Runbook", kind="gap",
                    issue="no runbook", improvement="write one")
    body = "---\nname: release-runbook\ndescription: run releases\n---\n# R\n\nSteps.\n"
    assert ss.dream_create_skill(tmp_path, "release-runbook", body, "seed").get("ok")
    assert _resolve_gap_observations(tmp_path) == 1
    assert not [r for r in open_observations(tmp_path)
                if r.get("skill") == "new:Release Runbook"]


def test_a_gap_brings_its_session_end_and_what_was_corrected_there(tmp_path):
    """A gap's text can carry the session's early, failed guesses; the facts
    that held are at that session's end and in the corrections logged from it."""
    sdir = tmp_path / "sessions"
    sdir.mkdir(parents=True)
    (sdir / "websocket_S1.jsonl").write_text(
        json.dumps({"role": "user", "content": "EARLY attempt with guessed columns"}) + "\n"
        + json.dumps({"role": "assistant",
                      "content": "FINAL_WORKING: log LIKE on logs_prod.containers"}) + "\n",
        encoding="utf-8")
    log_observation(tmp_path, skill="mxhero-support-api", kind="correction",
                    issue="contextId is nested in the log JSON",
                    improvement="use json_extract_scalar(log, '$.contextId')",
                    session="websocket_S1")
    log_observation(tmp_path, skill="new:athena-logs", kind="gap",
                    issue="no skill runs Athena queries",
                    improvement="create athena-logs with level and contextId columns",
                    session="websocket:S1")
    log_observation(tmp_path, skill="new:athena-logs", kind="gap",
                    issue="Athena was queried by hand again",
                    improvement="create athena-logs with level and contextId columns",
                    session="websocket:S2")

    msgs = _skill_extract_messages(tmp_path)

    user = msgs[1]["content"]
    assert "FINAL_WORKING: log LIKE on logs_prod.containers" in user
    assert "json_extract_scalar(log, '$.contextId')" in user
    assert "only facts" in msgs[0]["content"].lower()


def test_a_gap_seen_once_waits(tmp_path):
    """One sighting says a procedure happened, not that it will again: the
    gap stays in the queue and the extractor is not handed it."""
    _gap(tmp_path, name="db-backup", issue="the database was backed up by hand")
    _gap(tmp_path, sessions=("websocket_a",))

    assert "release-runbook" not in _skill_extract_messages(tmp_path)[-1]["content"]
    assert "new:release-runbook" in [r["skill"] for r in open_observations(tmp_path)]


def test_only_waiting_gaps_is_nothing_to_mine(tmp_path):
    _gap(tmp_path, sessions=("websocket_a",))

    assert _skill_extract_messages(tmp_path) is None


def test_a_gap_seen_again_under_other_words_is_ready(tmp_path):
    """A recurrence is logged under the same name with a fresh issue text, so
    it lands as a second record. The two are one gap, seen twice, and the
    extractor gets a single line for it."""
    _gap(tmp_path, issue="no skill covers releases", sessions=("websocket_a",))
    _gap(tmp_path, name="Release Runbook",
         issue="the wheel was built and installed by hand once more",
         sessions=("websocket_b",))
    assert len(open_observations(tmp_path)) == 2

    user = _skill_extract_messages(tmp_path)[-1]["content"]

    assert user.count("new:release-runbook") == 1
    assert "seen on 2 occasion(s)" in user
    assert "built and installed by hand once more" in user


def test_one_long_session_counts_by_the_days_it_was_seen_on(tmp_path, monkeypatch):
    """A chat kept open for weeks is a single session; a procedure done in it
    on two different days was still done twice."""
    from durin.agent import skill_observations as so

    monkeypatch.setattr(so, "_today", lambda: "2026-03-01")
    _gap(tmp_path, sessions=("websocket_a",))
    assert _skill_extract_messages(tmp_path) is None

    monkeypatch.setattr(so, "_today", lambda: "2026-03-09")
    _gap(tmp_path, sessions=("websocket_a",))

    assert "release-runbook" in _skill_extract_messages(tmp_path)[-1]["content"]


def test_two_slices_of_one_session_on_one_night_are_one_occasion(tmp_path):
    _gap(tmp_path, issue="no skill covers releases", sessions=("websocket_a",))
    _gap(tmp_path, issue="release steps were improvised a second time",
         sessions=("websocket:a",))

    assert _skill_extract_messages(tmp_path) is None


def test_the_bar_is_a_setting(tmp_path):
    _gap(tmp_path, sessions=("websocket_a",))

    msgs = _skill_extract_messages(tmp_path, min_gap_occasions=1)

    assert "release-runbook" in msgs[-1]["content"]


def test_one_ready_gap_gets_long_session_ends(tmp_path):
    """The gap's sessions are the only place the procedure is shown, so they
    share the whole evidence budget instead of a fixed short tail each."""
    from durin.memory import dream_passes

    marker = "STEP_THAT_WORKED "
    _session(tmp_path, "websocket_a",
             marker + "x" * (dream_passes._GAP_SESSION_TAIL_CHARS + 1500))
    _gap(tmp_path)

    assert marker in _skill_extract_messages(tmp_path)[-1]["content"]


def test_two_long_sessions_of_one_gap_both_arrive(tmp_path):
    """Each session end is as long as its share of the budget; together they
    fill it, and both must still reach the extractor."""
    from durin.memory import dream_passes

    long = "x" * dream_passes._GAP_EVIDENCE_TOTAL_CHARS
    _session(tmp_path, "websocket_a", long + " FIRST_SESSION_END")
    _session(tmp_path, "websocket_b", long + " SECOND_SESSION_END")
    _gap(tmp_path)

    user = _skill_extract_messages(tmp_path)[-1]["content"]

    assert "FIRST_SESSION_END" in user and "SECOND_SESSION_END" in user


def test_gaps_past_the_evidence_budget_keep_their_line(tmp_path):
    from durin.memory import dream_passes

    long = "x" * dream_passes._GAP_EVIDENCE_TOTAL_CHARS
    names = [f"procedure-{i}" for i in range(8)]
    for i, name in enumerate(names):
        _session(tmp_path, f"websocket_{i}a", long + f" END_{i}A")
        _session(tmp_path, f"websocket_{i}b", long + f" END_{i}B")
        _gap(tmp_path, name=name, issue=f"alpha{i} beta{i} gamma{i} delta{i} done by hand",
             sessions=(f"websocket_{i}a", f"websocket_{i}b"))

    user = _skill_extract_messages(tmp_path)[-1]["content"]

    assert all(f"new:{name}:" in user for name in names)
    assert "END_" in user
    assert len(user) < 3 * dream_passes._GAP_EVIDENCE_TOTAL_CHARS
