"""Hindsight skill-signal extraction — the dream detects skill corrections/gaps
from a session's turns and feeds the observation queue (no agent initiative)."""
import json

from durin.agent.skill_observations import open_observations
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
