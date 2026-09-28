"""Hindsight skill-signal extraction — the dream detects skill corrections/gaps
from a session's turns and feeds the observation queue (no agent initiative)."""
import json

from durin.agent.skill_observations import log_observation, open_observations
from durin.agent.skill_signals import (
    build_skill_signal_prompt,
    discover_skill_signals,
    parse_skill_signals,
)


def _stub(text):
    def inv(prompt, **kw):
        return text
    return inv


# --- parse_skill_signals ----------------------------------------------------

def test_parse_skill_signals_validates_and_normalizes_gap_prefix():
    raw = (
        "```json\n"
        '[{"skill":"git-helper","kind":"correction","issue":"step 2 wrong",'
        '"improvement":"prefer rebase"},'
        ' {"skill":"deploy","kind":"gap","issue":"no skill covers it",'
        '"improvement":"author a deploy flow"},'
        ' {"skill":"x","kind":"correction","issue":"","improvement":"y"},'
        ' {"skill":"z","kind":"bogus","issue":"i","improvement":"m"}]\n'
        "```"
    )
    # empty-field and bad-kind items dropped; gap normalized to new:<name>
    assert parse_skill_signals(raw) == [
        {"skill": "git-helper", "kind": "correction",
         "issue": "step 2 wrong", "improvement": "prefer rebase"},
        {"skill": "new:deploy", "kind": "gap",
         "issue": "no skill covers it", "improvement": "author a deploy flow"},
    ]


def test_parse_skill_signals_non_list_is_empty():
    assert parse_skill_signals('{"a": 1}') == []
    assert parse_skill_signals("no json here") == []


# --- build_skill_signal_prompt ----------------------------------------------

def test_build_prompt_includes_turn_indexed_loads_header():
    p = build_skill_signal_prompt(
        "the turns", [{"skill": "git-helper", "op": "read", "turn": 3}])
    assert "git-helper@3" in p
    assert "the turns" in p


def test_a_skill_loaded_with_skill_view_counts_as_loaded():
    """skill_view is the dedicated load tool; a skill it loaded must not look
    unloaded, or the pass reports a procedure that skill covers as a gap."""
    p = build_skill_signal_prompt(
        "the turns", [{"skill": "mxhero-support-api", "op": "view", "turn": 3}])
    assert "mxhero-support-api@3" in p
    assert "(none recorded)" not in p


def test_the_signal_pass_sees_the_catalog_and_open_gaps(tmp_path):
    """A gap is only for work no skill covers; the pass can only tell when it
    sees what exists, and can reuse an open gap's name for the same work."""
    skill = tmp_path / "skills" / "mxhero-support-api" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: mxhero-support-api\ndescription: Query mxHero APIs and "
                     "container logs in Athena\n---\n# API\n", encoding="utf-8")
    log_observation(tmp_path, skill="new:release-runbook", kind="gap",
                    issue="no skill covers releases", improvement="write one")
    prompts = []

    def _capture(prompt, **kw):
        prompts.append(prompt)
        return "[]"

    discover_skill_signals(tmp_path, "USER: trace this email", llm_invoke=_capture)

    assert "mxhero-support-api" in prompts[0]
    assert "container logs in Athena" in prompts[0]
    assert "release-runbook" in prompts[0]


def test_a_gap_named_after_an_existing_skill_becomes_an_improvement_on_it(tmp_path):
    skill = tmp_path / "skills" / "athena-boto3-query" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: athena-boto3-query\ndescription: run Athena SQL\n---\n# q\n",
                     encoding="utf-8")

    res = log_observation(tmp_path, skill="new:athena-boto3-query", kind="gap",
                          issue="needs a way to run Athena SQL", improvement="add defaults")

    assert res.get("ok"), res
    [rec] = open_observations(tmp_path)
    assert rec["skill"] == "athena-boto3-query"
    assert rec["kind"] == "improvement"


def test_prompt_keeps_most_recent_turns_when_truncating():
    # Corrections land at the END of an interaction, so truncation must keep the
    # tail, not the head (unlike entity discovery, which head-truncates).
    turns = "EARLY_MARKER " + ("x" * 13000) + " LATE_MARKER"
    p = build_skill_signal_prompt(turns, [])
    assert "LATE_MARKER" in p
    assert "EARLY_MARKER" not in p


# --- discover_skill_signals -------------------------------------------------

def test_discover_skill_signals_logs_observations(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    raw = ('[{"skill":"git-helper","kind":"correction",'
           '"issue":"used merge not rebase","improvement":"prefer rebase in step 2"}]')
    out = discover_skill_signals(
        ws, "USER: no, rebase\nTOOL: name: git-helper",
        skill_loads=[{"skill": "git-helper", "op": "read", "turn": 2}],
        llm_invoke=_stub(raw))
    assert len(out) == 1
    assert out[0]["skill"] == "git-helper"
    obs = open_observations(ws, skill="git-helper")
    assert len(obs) == 1
    assert obs[0]["kind"] == "correction"
    assert "rebase" in obs[0]["improvement"]


def test_discover_skill_signals_empty_turns_makes_no_call(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    def boom(prompt, **kw):
        raise AssertionError("LLM must not be called for empty turns")

    assert discover_skill_signals(ws, "   ", llm_invoke=boom) == []


# --- wiring: stage 3 of the extract dream -----------------------------------

def test_run_extract_for_session_logs_skill_signals(tmp_path):
    from durin.memory.extract_runner import run_extract_for_session

    ws = tmp_path / "ws"
    ws.mkdir()
    sdir = ws / "sessions"
    sdir.mkdir()
    p = sdir / "s1.jsonl"
    rows = [
        {"_type": "metadata", "key": "s1"},
        {"role": "user", "content": "do a git flow"},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "read_file",
                          "arguments": '{"path": "skills/git-helper/SKILL.md"}'}}]},
        {"role": "user", "content": "no, rebase not merge"},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    signal = ('[{"skill":"git-helper","kind":"correction",'
              '"issue":"used merge not rebase","improvement":"prefer rebase in step 2"}]')
    res = run_extract_for_session(
        ws, p, llm_invoke=_stub(signal), discover=False, skill_signals=True)
    assert res["skill_signals"]
    assert any(o["skill"] == "git-helper" for o in open_observations(ws))


def test_run_extract_for_session_skill_signals_off_logs_nothing(tmp_path):
    from durin.memory.extract_runner import run_extract_for_session

    ws = tmp_path / "ws"
    ws.mkdir()
    sdir = ws / "sessions"
    sdir.mkdir()
    p = sdir / "s2.jsonl"
    rows = [
        {"_type": "metadata", "key": "s2"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok"},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")

    def boom(prompt, **kw):
        raise AssertionError("no LLM call when both stages are off")

    res = run_extract_for_session(
        ws, p, llm_invoke=boom, discover=False, skill_signals=False)
    assert res["skill_signals"] == []
    assert open_observations(ws) == []
