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
    assert [d for n, d in events if n == "skill.curation_unrecovered"][-1] == {
        "stage": "curation", "failed": 0, "stalled": 1}

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


def test_a_provider_error_tied_to_one_skill_is_split_down_to_it(tmp_path, monkeypatch):
    """A provider refuses some requests for their content — past the model's
    context, caught by an input filter — and its retries give up at once, as
    finish_reason "error". The batch holding that skill is split like any
    refused batch, so the skill cannot stop the review of everything after it,
    and its own failures set it aside."""
    events = _events(monkeypatch)
    ws = tmp_path / "ws"
    names = [f"a-{i:02d}" for i in range(30)]
    for n in names:
        _mk(ws, n)

    def judge(prompt):
        if '"a-10"' in prompt:
            return LLMResponse(text="Error: 400 This model's maximum context length is "
                                    "65536 tokens", finish_reason="error")
        return LLMResponse(text=_EMPTY)

    res = sc.curate_catalog(ws, judge=judge)
    assert res["failed"] == 1
    assert [n for n in names if ss.needs_curation(ws, n)] == ["a-10"]

    for _ in range(sc._STALL_REVIEWS - 1):
        sc.curate_catalog(ws, judge=judge)
    stalled = [d for n, d in events if n == "skill.curation_stalled"]
    assert [d["skill"] for d in stalled] == ["a-10"]


def test_a_batch_whose_request_times_out_is_split_until_the_answers_come_in_time(tmp_path):
    """A slow model cannot write a whole batch's answer before the request
    times out; the provider's retries end in finish_reason "error". Smaller
    batches answer in time, so the pass still reviews everything."""
    ws = tmp_path / "ws"
    names = [f"a-{i:02d}" for i in range(20)]
    for n in names:
        _mk(ws, n)

    def slow(prompt):
        if sum(f'"{n}"' in prompt for n in names) > 1:
            return LLMResponse(text="Request timed out.", finish_reason="error")
        return LLMResponse(text=_EMPTY)

    res = sc.curate_catalog(ws, judge=slow)

    assert res["reviewed"] == 20 and "failed" not in res
    assert not any(ss.needs_curation(ws, n) for n in names)


@pytest.mark.parametrize("reply", [
    LLMResponse(text="Error calling LLM: 503", finish_reason="error"),
    LLMResponse(text="I cannot do that."),
    LLMResponse(text='{"actions": [{"type": "evolve", "name": "x", "old": "a", "new": "b',
                finish_reason="length"),
])
def test_a_model_that_answers_nothing_usable_ends_the_pass_early(tmp_path, monkeypatch, reply):
    """An outage, or a preset whose answers never parse, fails every call:
    splitting down to single skills only multiplies calls that fail. The pass
    ends after a few single-skill reviews in a row got nothing usable, and the
    rest carries over, charged to no skill."""
    events = _events(monkeypatch)
    ws = tmp_path / "ws"
    names = [f"a-{i:02d}" for i in range(50)]
    for n in names:
        _mk(ws, n)
    shown: list[int] = []

    def judge(prompt):
        shown.append(sum(f'"{n}"' in prompt for n in names))
        return reply

    res = sc.curate_catalog(ws, judge=judge)

    assert shown.count(1) == sc._FAILED_IN_A_ROW
    # The first batch split down to single skills (8, 4, 2, 1, 1, then its
    # next quarter 2, 1) — not the whole selection's 2n - 1 calls.
    assert len(shown) == 7
    assert res["failed"] == 50
    assert all(ss.needs_curation(ws, n) for n in names)
    records = json.loads((ws / "skills" / sc._FAILURES).read_text(encoding="utf-8"))
    assert len(records["curation"]) == sc._FAILED_IN_A_ROW
    assert [d for n, d in events if n == "skill.curation_unrecovered"] == [{
        "stage": "curation", "failed": sc._FAILED_IN_A_ROW, "stalled": 0,
        "ended_early": "model_failing", "carried_over": 50 - sc._FAILED_IN_A_ROW}]

    res = sc.curate_catalog(ws, judge=lambda p: _EMPTY)
    assert res["reviewed"] == 50 and "failed" not in res


def test_skills_whose_review_failed_go_after_the_others(tmp_path, monkeypatch):
    """Failing skills never stand in front of the rest: a pass that ends early
    on them has reviewed everything else first."""
    ws = tmp_path / "ws"
    bad = ["a-00", "a-01", "a-02"]
    for n in bad:
        _mk(ws, n)
    # Listed first, as the directory order may well put them.
    listed = ss.list_skills_info
    monkeypatch.setattr(ss, "list_skills_info",
                        lambda w: sorted(listed(w), key=lambda s: s["name"]))

    def judge(prompt):
        return "I cannot review this." if any(f'"{n}"' in prompt for n in bad) else _EMPTY

    sc.curate_catalog(ws, judge=judge)
    assert all(ss.needs_curation(ws, n) for n in bad)

    fresh = [f"b-{i:02d}" for i in range(8)]
    for n in fresh:
        _mk(ws, n)
    res = sc.curate_catalog(ws, judge=judge)

    assert res["reviewed"] == 11
    assert not any(ss.needs_curation(ws, n) for n in fresh)


def test_skills_that_fail_apart_do_not_end_the_pass(tmp_path, monkeypatch):
    """The early end counts single-skill failures in a row: an answer used in
    between shows the model working, so skills that fail here and there never
    stop the review of the rest."""
    ws = tmp_path / "ws"
    names = [f"a-{i:02d}" for i in range(40)]
    for n in names:
        _mk(ws, n)
    listed = ss.list_skills_info
    monkeypatch.setattr(ss, "list_skills_info",
                        lambda w: sorted(listed(w), key=lambda s: s["name"]))
    bad = ["a-00", "a-08", "a-16"]  # the first skill of three batches of eight

    def judge(prompt):
        return "I cannot review this." if any(f'"{n}"' in prompt for n in bad) else _EMPTY

    res = sc.curate_catalog(ws, judge=judge)

    assert res["failed"] == len(bad)
    assert [n for n in names if ss.needs_curation(ws, n)] == bad


def test_a_cross_skill_record_is_answered_by_one_batch(tmp_path):
    """A cross-skill ("all") record rides with the batches until one batch's
    answer is used — a refused answer passes it on — and is disposed by that
    answer alone: shown to every batch, one pass would note an attempt per
    batch and stall the record in a night."""
    ws = tmp_path / "ws"
    names = [f"a-{i:02d}" for i in range(20)]
    for n in names:
        _mk(ws, n)
    issue = "every script skips a dry run"
    oid = so.log_observation(ws, skill="all", kind="improvement", issue=issue,
                             improvement="offer a dry run")["id"]
    shown: list[bool] = []

    def judge(prompt):
        shown.append(issue in prompt)
        if len(shown) == 1:
            return LLMResponse(text='{"actions": [', finish_reason="length")
        # Marked applied with nothing landed: kept, with the attempt noted.
        return LLMResponse(text=json.dumps({"actions": [], "observations": (
            [{"id": oid, "disposition": "applied"}] if shown[-1] else [])}))

    sc.curate_catalog(ws, judge=judge)

    assert shown[:2] == [True, True] and not any(shown[2:])
    [rec] = so.open_observations(ws, skill="all")
    assert len(rec["attempts"]) == 1 and not rec.get("stalled_at")


def _feed(events: list) -> list[dict]:
    """The Dream feed items these telemetry events make."""
    from durin.memory.dream_digest import DREAM_ACTIVITY_TYPES, map_dream_event
    return [item for name, data in events if name in DREAM_ACTIVITY_TYPES
            for item in map_dream_event(name, data, 0)]


def test_the_dream_feed_shows_no_refusal_the_split_recovered(tmp_path, monkeypatch):
    """A batch cut at the output limit and reviewed in full once split is
    routine: its refusals stay telemetry rows, and the feed warns of nothing."""
    events = _events(monkeypatch)
    ws = tmp_path / "ws"
    names = [f"skill-{i:02d}" for i in range(4)]
    for i, n in enumerate(names):
        _mk(ws, n, _spanish(i))

    sc.curate_catalog(ws, judge=_Model(names, limit=4_000))

    assert [d for n, d in events if n == "memory.dream.parse_failure"]
    assert not [i for i in _feed(events) if i["kind"] == "warning"]


def test_the_dream_feed_shows_what_a_pass_could_not_recover_in_one_line(tmp_path, monkeypatch):
    """However many calls were refused on the way down to them, two skills
    whose own review fails are one line for the pass."""
    events = _events(monkeypatch)
    ws = tmp_path / "ws"
    names = [f"a-{i:02d}" for i in range(20)]
    for n in names:
        _mk(ws, n)

    def judge(prompt):
        return "I cannot review this." if '"a-03"' in prompt or '"a-12"' in prompt else _EMPTY

    sc.curate_catalog(ws, judge=judge)

    assert len([d for n, d in events if n == "memory.dream.parse_failure"]) > 2
    assert [i["summary"] for i in _feed(events) if i["kind"] == "warning"] == [
        "Skill curation: 2 skill(s) got no usable answer"]


@pytest.mark.parametrize("finish", ["length", "content_filter", "refusal",
                                    "model_context_window_exceeded"])
def test_an_answer_the_model_did_not_finish_is_never_applied(tmp_path, finish):
    """Only an answer that ran to its end ("stop") is parsed. Any other finish
    reason — cut at the output limit, filtered, refused — may come with a
    partial answer, which JSON repair would turn into an edit whose
    replacement stops mid-text."""
    ws = tmp_path / "ws"
    _mk(ws, "alpha", _spanish(0))
    cut = json.dumps({"actions": [{"type": "evolve", "name": "alpha", "old": _spanish(0),
                                   "new": _english(0)}]})[:-60]

    res = sc.curate_catalog(ws, judge=lambda p: LLMResponse(text=cut, finish_reason=finish))

    assert res["applied"] == 0 and res["failed"] == 1
    assert _spanish(0) in (ss.read_skill_content(ws, "alpha") or "")
    assert ss.needs_curation(ws, "alpha")


def test_an_answer_with_null_lists_is_a_review_with_nothing_to_do(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "alpha")

    res = sc.curate_catalog(ws, judge=lambda p: '{"actions": null, "observations": null}')

    assert res["reviewed"] == 1 and "failed" not in res
    assert not ss.needs_curation(ws, "alpha")


@pytest.mark.parametrize("bad", [
    '{"actions": ["evolve a-00"], "observations": []}',
    '{"actions": {"type": "retire", "name": "a-00"}, "observations": []}',
    '{"actions": [], "observations": [3]}',
    '{"actions": [], "observations": [{"id": [1], "disposition": "applied"}]}',
])
def test_a_malformed_answer_fails_its_batch_and_not_the_pass(tmp_path, monkeypatch, bad):
    """An answer whose lists hold something else than the objects the prompt
    asks for fails that batch — split and retried like an unparseable one —
    and never raises out of the pass, taking the other batches with it."""
    events = _events(monkeypatch)
    ws = tmp_path / "ws"
    names = [f"a-{i:02d}" for i in range(20)]
    for n in names:
        _mk(ws, n)

    res = sc.curate_catalog(ws, judge=lambda p: bad if '"a-00"' in p else _EMPTY)

    assert res["failed"] == 1
    assert [n for n in names if ss.needs_curation(ws, n)] == ["a-00"]
    assert [d for n, d in events if n == "skill.curation_run"][-1]["failed"] == 1


def test_each_batch_sees_the_principles_the_batches_before_it_left(tmp_path):
    """A principle one batch's answer adds or retires is in force for the
    batches after it: a retired one is no longer a rule to evolve skills
    toward, and an added one is not proposed again in other words."""
    ws = tmp_path / "ws"
    names = [f"a-{i:02d}" for i in range(12)]
    for n in names:
        _mk(ws, n)
    assert so.add_principle(ws, "Keep every step numbered.", rationale="seed").get("ok")
    prompts: list[str] = []

    def judge(prompt):
        prompts.append(prompt)
        if len(prompts) > 1:
            return _EMPTY
        return json.dumps({"actions": [
            {"type": "retire_principle", "id": 1},
            {"type": "principle", "text": "Write every skill in English.",
             "rationale": "mixed languages"}], "observations": []})

    sc.curate_catalog(ws, judge=judge)

    assert len(prompts) == 2
    assert "Keep every step numbered." in prompts[0]
    assert "Keep every step numbered." not in prompts[1]
    assert "Write every skill in English." in prompts[1]


def test_the_pass_stops_starting_batches_once_its_time_is_up(tmp_path, monkeypatch):
    """Like every dream pass, the review honors memory.dream.max_seconds_per_run:
    once the time is up it starts no new batch; the rest carries over, charged
    to no skill."""
    events = _events(monkeypatch)
    ws = tmp_path / "ws"
    names = [f"a-{i:02d}" for i in range(50)]
    for n in names:
        _mk(ws, n)
    now = [1000.0]
    monkeypatch.setattr(sc, "_clock", lambda: now[0])

    def judge(prompt):
        now[0] += 100
        return _EMPTY

    res = sc.curate_catalog(ws, judge=judge, max_seconds=250)

    owed = [n for n in names if ss.needs_curation(ws, n)]
    assert len(owed) == 50 - 3 * sc._BATCH_SKILLS
    assert res["failed"] == len(owed)
    assert not (ws / "skills" / sc._FAILURES).exists()
    [cap] = [d for n, d in events if n == "memory.dream.max_seconds_reached"]
    assert cap["kind"] == "curation" and cap["remaining"] == len(owed)
    assert [d for n, d in events if n == "skill.curation_unrecovered"] == [{
        "stage": "curation", "failed": 0, "stalled": 0,
        "ended_early": "time_cap", "carried_over": len(owed)}]


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


def test_suggestions_split_a_cut_batch_and_skip_a_failing_skill(tmp_path, monkeypatch):
    events = _events(monkeypatch)
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
    assert [d for n, d in events if n == "skill.curation_unrecovered"] == [
        {"stage": "suggestions", "failed": 1, "stalled": 0}]


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


def test_suggestions_stop_starting_batches_once_their_time_is_up(tmp_path, monkeypatch):
    """The manual-skill pass honors the time cap too, and a cap reached
    exactly is reached: no batch starts past it."""
    events = _events(monkeypatch)
    ws = tmp_path
    names = [f"m-{i:02d}" for i in range(20)]
    for n in names:
        _manual(ws, n)
    now = [1000.0]
    monkeypatch.setattr(sc, "_clock", lambda: now[0])
    calls: list[str] = []

    def judge(prompt):
        calls.append(prompt)
        now[0] += 100
        return _EMPTY

    out = sc.suggest_manual_skills(ws, judge=judge, max_seconds=100)

    assert len(calls) == 1
    left = len(names) - sc._BATCH_SKILLS
    assert out["failed"] == left
    assert sum(sg.needs_suggestion(ws, n) for n in names) == left
    assert [d for n, d in events if n == "skill.curation_unrecovered"] == [{
        "stage": "suggestions", "failed": 0, "stalled": 0,
        "ended_early": "time_cap", "carried_over": left}]
