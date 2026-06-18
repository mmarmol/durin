"""Hindsight skill-signal extraction — the dream detects skill corrections/gaps
from a session's turns and feeds the observation queue (no agent initiative)."""
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
