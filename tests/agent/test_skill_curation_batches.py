"""The curation review makes progress however large the delta is.

The judge answers one JSON object for every skill it is shown, and an
`evolve` carries the text it replaces and its replacement — so the answer
grows with the review set until the provider cuts it at the output limit.
These tests drive `curate_catalog` / `suggest_manual_skills` with a fake
model that behaves like a provider: an answer past its limit comes back cut,
with finish_reason "length".
"""
import json
from datetime import date, timedelta

import pytest

from durin.agent import skill_curation as sc
from durin.agent import skill_observations as so
from durin.agent import skill_suggestions as sg
from durin.agent import skills_store as ss
from durin.memory.llm_invoke import LLMResponse

_EMPTY = '{"actions": [], "observations": []}'


@pytest.fixture(autouse=True)
def _no_skill_index(monkeypatch):
    """Every edit and stamp re-indexes its skill (an embedding per commit);
    at fifty skills that is minutes of work these tests do not look at."""
    monkeypatch.setattr(ss, "_index_skills_enabled", lambda: False)


def _mk(ws, name, body="body"):
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\nmetadata:\n  durin:\n    mode: auto\n"
        f"    provenance:\n      source: dream\n---\n{body}\n", encoding="utf-8")


def _spanish(i: int) -> str:
    return f"Paso {i:02d}: " + "revisa el registro y anota el resultado. " * 25


def _english(i: int) -> str:
    return f"Step {i:02d}: " + "check the log and write down the result. " * 25


def _events(monkeypatch) -> list:
    import durin.agent.tools._telemetry as tel
    events: list = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    return events


class _Model:
    """A judge with an output limit. It answers each skill shown to it with an
    English-normalization `evolve` (old text + replacement: about twice the
    skill), and an answer longer than ``limit`` chars comes back cut with
    finish_reason "length", as a provider does at its max_tokens."""

    def __init__(self, names: list[str], limit: int):
        self.names = names
        self.limit = limit
        self.shown: list[list[str]] = []
        self.cut = 0

    def answer(self, shown: list[str]) -> str:
        return json.dumps({"actions": [
            {"type": "evolve", "name": n, "old": _spanish(self.names.index(n)),
             "new": _english(self.names.index(n)), "rationale": "English normalization"}
            for n in shown], "observations": []}, ensure_ascii=False)

    def __call__(self, prompt: str) -> LLMResponse:
        shown = [n for n in self.names if f'"{n}"' in prompt]
        self.shown.append(shown)
        text = self.answer(shown)
        if len(text) > self.limit:
            self.cut += 1
            return LLMResponse(text=text[:self.limit], finish_reason="length")
        return LLMResponse(text=text)


def test_a_delta_too_large_for_one_answer_is_reviewed_in_batches(tmp_path):
    ws = tmp_path / "ws"
    names = [f"skill-{i:02d}" for i in range(50)]
    for i, n in enumerate(names):
        _mk(ws, n, _spanish(i))
    # About 8,192 tokens of output: the whole delta in one answer is far past it.
    model = _Model(names, limit=24_000)
    assert len(model.answer(names)) > 4 * model.limit

    res = sc.curate_catalog(ws, judge=model)

    assert res["reviewed"] == 50 and res["applied"] == 50
    assert "judge_parse_failed" not in res
    # Batches are sized so the answers fit: none was cut, each skill judged once.
    assert model.cut == 0
    assert sorted(n for shown in model.shown for n in shown) == names
    for i, n in enumerate(names):
        body = ss.read_skill_content(ws, n) or ""
        assert _english(i) in body and _spanish(i) not in body
        assert not ss.needs_curation(ws, n)


def test_a_batch_whose_answer_is_cut_is_split_and_never_applied(tmp_path, monkeypatch):
    events = _events(monkeypatch)
    ws = tmp_path / "ws"
    names = [f"skill-{i:02d}" for i in range(4)]
    for i, n in enumerate(names):
        _mk(ws, n, _spanish(i))
    model = _Model(names, limit=4_000)
    # The premise: repaired, the cut answer reads as a complete edit whose
    # replacement text stops mid-sentence. Applying it would corrupt the skill.
    fragment, _ = sc._parse_judge_output(model.answer(names)[:model.limit])
    assert any(a.get("new") and a["new"] not in [_english(i) for i in range(4)]
               for a in fragment["actions"])

    res = sc.curate_catalog(ws, judge=model)

    assert [len(s) for s in model.shown][:2] == [4, 2]
    assert res["applied"] == 4 and "judge_parse_failed" not in res
    for i, n in enumerate(names):
        body = ss.read_skill_content(ws, n) or ""
        assert _english(i) in body and _spanish(i) not in body
        assert not ss.needs_curation(ws, n)
    failures = [d for n, d in events if n == "memory.dream.parse_failure"]
    assert failures and all(d["finish_reason"] == "length" for d in failures)
    assert failures[0]["raw_len"] == model.limit
    assert failures[0]["raw_tail"] == model.answer(names)[:model.limit][-200:]


def test_a_skill_whose_review_always_fails_does_not_hold_back_the_others(tmp_path, monkeypatch):
    events = _events(monkeypatch)
    ws = tmp_path / "ws"
    for n in ("alpha", "bravo", "charlie", "delta"):
        _mk(ws, n)

    def judge(prompt):
        return "I cannot review charlie, sorry." if '"charlie"' in prompt else _EMPTY

    res = sc.curate_catalog(ws, judge=judge)

    assert res["reviewed"] == 4
    assert res["failed"] == 1 and res["judge_parse_failed"] is True
    assert ss.needs_curation(ws, "charlie")
    for n in ("alpha", "bravo", "delta"):
        assert not ss.needs_curation(ws, n)
    failures = [d for n, d in events if n == "memory.dream.parse_failure"]
    assert failures and all(d["stage"] == "curation" and d.get("error") for d in failures)
    runs = [d for n, d in events if n == "skill.curation_run"]
    assert runs[-1]["failed"] == 1


def test_a_skill_that_fails_alone_pass_after_pass_is_set_aside(tmp_path, monkeypatch):
    events = _events(monkeypatch)
    ws = tmp_path / "ws"
    _mk(ws, "alpha")
    _mk(ws, "charlie")
    seen: list[bool] = []

    def judge(prompt):
        seen.append('"charlie"' in prompt)
        return "prose" if '"charlie"' in prompt else _EMPTY

    for _ in range(sc._STALL_REVIEWS):
        sc.curate_catalog(ws, judge=judge)
    stalled = [d for n, d in events if n == "skill.curation_stalled"]
    assert stalled == [{"stage": "curation", "skill": "charlie",
                        "failures": sc._STALL_REVIEWS}]

    # Set aside: not reviewed again, and still owed a review (not stamped).
    seen.clear()
    res = sc.curate_catalog(ws, judge=judge)
    assert not any(seen)
    assert res["reviewed"] == 0 and res["stalled"] == 1
    assert ss.needs_curation(ws, "charlie")

    # Once the window has passed it is tried again — and a failure sets it
    # aside again at once.
    later = date.today() + timedelta(days=sc._STALL_DAYS)
    monkeypatch.setattr(sc, "_today", lambda: later)
    seen.clear()
    sc.curate_catalog(ws, judge=judge)
    assert any(seen)
    seen.clear()
    sc.curate_catalog(ws, judge=judge)
    assert not any(seen)

    # A changed body is a different review: it re-enters at once.
    ss.save_skill_file(ws, "charlie", "SKILL.md",
                       "---\nname: charlie\ndescription: charlie skill\nmetadata:\n"
                       "  durin:\n    mode: auto\n---\nnew body\n",
                       rationale="edited", attribution=ss.Attribution(actor="user"))
    seen.clear()
    sc.curate_catalog(ws, judge=judge)
    assert any(seen)


def test_a_review_that_succeeds_clears_the_failure_count(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "charlie")
    answers = iter(["prose", "prose", _EMPTY, "prose", "prose", _EMPTY])
    seen: list[bool] = []

    def judge(prompt):
        seen.append('"charlie"' in prompt)
        return next(answers)

    sc.curate_catalog(ws, judge=judge)
    sc.curate_catalog(ws, judge=judge)
    sc.curate_catalog(ws, judge=judge)  # succeeds: stamped
    assert not ss.needs_curation(ws, "charlie")
    # An open observation pulls the unchanged skill back in; two more failures
    # are two in a row, not four.
    assert so.log_observation(ws, skill="charlie", kind="gap", issue="misses a step",
                              improvement="add the step").get("ok")
    sc.curate_catalog(ws, judge=judge)
    sc.curate_catalog(ws, judge=judge)
    seen.clear()
    sc.curate_catalog(ws, judge=judge)
    assert seen == [True]


def test_a_provider_error_ends_the_pass_and_counts_against_no_skill(tmp_path, monkeypatch):
    events = _events(monkeypatch)
    ws = tmp_path / "ws"
    names = [f"skill-{i:02d}" for i in range(12)]
    for n in names:
        _mk(ws, n)
    calls: list[str] = []

    def down(prompt):
        calls.append(prompt)
        return LLMResponse(text="Error calling LLM: 503", finish_reason="error")

    for _ in range(sc._STALL_REVIEWS):
        res = sc.curate_catalog(ws, judge=down)
    # One call per pass: an outage is not split, nor carried to the next batch.
    assert len(calls) == sc._STALL_REVIEWS
    assert res["failed"] == 12
    failures = [d for n, d in events if n == "memory.dream.parse_failure"]
    assert failures and all(d["finish_reason"] == "error" for d in failures)
    assert not [d for n, d in events if n == "skill.curation_stalled"]

    res = sc.curate_catalog(ws, judge=lambda p: _EMPTY)
    assert res["reviewed"] == 12 and "failed" not in res
    assert not any(ss.needs_curation(ws, n) for n in names)


@pytest.mark.parametrize("gate, lands", [
    ("COMPLIANT", True),
    ("NARRATION — narrates a workflow-shaped procedure", False),
])
def test_a_fuse_keeps_its_composition_gate_when_the_judge_returns_a_response(
        tmp_path, gate, lands):
    """The judge also gates a fuse's merged body. Handed the provider response
    instead of its text, the gate would read a repr, fail open, and let a
    doctrine violation through."""
    ws = tmp_path / "ws"
    _mk(ws, "git-a", "git rebase steps")
    _mk(ws, "git-b", "git rebase steps too")
    fuse = json.dumps({"actions": [{
        "type": "fuse", "target": "git-flow", "sources": ["git-a", "git-b"],
        "content": "---\nname: git-flow\ndescription: Merged git flow skill.\n---\n"
                   "# Git flow\n\nmerged\n",
        "rationale": "same steps"}], "observations": []})

    def judge(prompt):
        if "composition doctrine before it is saved" in prompt:
            return LLMResponse(text=gate)
        return LLMResponse(text=fuse)

    res = sc.curate_catalog(ws, judge=judge)
    assert res["applied"] == (1 if lands else 0)
    assert (ws / "skills" / "git-flow").exists() is lands


def _manual(ws, name, body="body"):
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\ndurin:\n  mode: manual\n---\n{body}\n",
        encoding="utf-8")


def test_suggestions_split_a_cut_batch_and_skip_a_failing_skill(tmp_path):
    ws = tmp_path
    names = ["m-a", "m-b", "m-bad", "m-c"]
    for n in names:
        _manual(ws, n)

    def judge(prompt):
        shown = [n for n in names if f'"{n}"' in prompt]
        if len(shown) > 2:
            return LLMResponse(text='{"actions": [{"type": "evolve", "name": "m-a", '
                                    '"old": "body", "new": "bo', finish_reason="length")
        if "m-bad" in shown:
            return LLMResponse(text="I would rather not.")
        return LLMResponse(text='{"actions": []}')

    out = sc.suggest_manual_skills(ws, judge=judge)

    assert out["reviewed"] == 4
    assert out["failed"] == 1 and out["judge_parse_failed"] is True
    assert sg.read_suggestions(ws) == []  # the cut answer's edit was never queued
    assert sg.needs_suggestion(ws, "m-bad")
    assert not any(sg.needs_suggestion(ws, n) for n in ("m-a", "m-b", "m-c"))


def test_suggestions_set_aside_a_skill_that_keeps_failing(tmp_path):
    ws = tmp_path
    _manual(ws, "m-bad")
    calls: list[str] = []

    def judge(prompt):
        calls.append(prompt)
        return "prose"

    for _ in range(sc._STALL_REVIEWS):
        sc.suggest_manual_skills(ws, judge=judge)
    calls.clear()
    out = sc.suggest_manual_skills(ws, judge=judge)
    assert calls == []
    assert out["reviewed"] == 0 and out["stalled"] == 1
