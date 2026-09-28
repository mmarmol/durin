"""Curation consumes the observation queue (task-observer pattern, part B).

OPEN observations pull their skill into the curation delta and reach the
judge as evidence; the judge's per-observation dispositions update the queue;
APPLIED records get one cycle of visibility then archive on the next run.
"""
import json

from durin.agent import skills_store as ss
from durin.agent.skill_curation import curate_catalog
from durin.agent.skill_observations import (
    active_principles,
    add_principle,
    log_observation,
    open_observations,
    suppressed_observations,
)


def _mk(ws, name, body="body"):
    d = ws / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\nmetadata:\n  durin:\n    mode: auto\n"
        f"    provenance:\n      source: dream\n---\n{body}\n", encoding="utf-8")


def _obs(ws, skill="stable", issue="wheel step is wrong", count=1):
    for _ in range(count):
        res = log_observation(ws, skill=skill, kind="correction", issue=issue,
                              improvement="build from local dist")
    return res


def test_open_observation_pulls_unchanged_skill_into_delta(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "stable")
    ss.mark_curated(ws, "stable")          # body unchanged → not in change-delta
    _obs(ws, skill="stable")

    calls = []
    res = curate_catalog(ws, judge=lambda p: calls.append(p) or '{"actions": []}')
    assert res["reviewed"] == 1
    assert "wheel step is wrong" in calls[0]


def test_no_observations_keeps_change_gate_unchanged(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "stable")
    ss.mark_curated(ws, "stable")

    calls = []
    res = curate_catalog(ws, judge=lambda p: calls.append(p) or '{"actions": []}')
    assert res["reviewed"] == 0
    assert calls == []


def test_judge_dispositions_update_queue(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "stable", "old step here")
    ss.mark_curated(ws, "stable")
    _obs(ws, skill="stable", count=2)      # recurring → judge acts

    def judge(prompt):
        return json.dumps({
            "actions": [{"type": "evolve", "name": "stable",
                         "old": "old step here", "new": "new step here",
                         "rationale": "obs #1"}],
            "observations": [{"id": 1, "disposition": "applied"}],
        })

    res = curate_catalog(ws, judge=judge)
    assert res["applied"] == 1
    assert open_observations(ws) == []
    assert "new step here" in ss.read_skill_content(ws, "stable")


def test_declined_disposition_remembered_and_shown_to_judge(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "stable")
    ss.mark_curated(ws, "stable")
    _obs(ws, skill="stable")

    curate_catalog(ws, judge=lambda p: json.dumps({
        "actions": [], "observations": [{"id": 1, "disposition": "declined"}]}))
    assert [r["id"] for r in suppressed_observations(ws)] == [1]

    # next run: a fresh OPEN obs triggers review; declined history is in prompt
    _obs(ws, skill="stable", issue="another problem entirely")
    calls = []
    curate_catalog(ws, judge=lambda p: calls.append(p) or '{"actions": []}')
    assert "wheel step is wrong" in calls[0]      # declined shown
    assert "declined" in calls[0].lower()


def test_applied_observations_archived_on_next_run(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "stable", "Step 2: build from local dist, never from PyPI.")
    ss.mark_curated(ws, "stable")
    _obs(ws, skill="stable")
    # Already incorporated: the judge quotes the text that shows it.
    curate_catalog(ws, judge=lambda p: json.dumps({
        "actions": [], "observations": [{"id": 1, "disposition": "applied",
                                         "evidence": "build from local dist"}]}))
    assert (ws / "skills" / ".observations.jsonl").read_text().count('"APPLIED"') == 1

    _obs(ws, skill="stable", issue="another problem entirely")
    curate_catalog(ws, judge=lambda p: '{"actions": []}')
    active = (ws / "skills" / ".observations.jsonl").read_text()
    assert '"APPLIED"' not in active
    archive = (ws / "skills" / ".observations.archive.jsonl").read_text()
    assert "wheel step is wrong" in archive


def _record(ws, oid):
    rows = [json.loads(line) for line in
            (ws / "skills" / ".observations.jsonl").read_text().splitlines() if line.strip()]
    return next(r for r in rows if r["id"] == oid)


def test_an_applied_claim_whose_change_did_not_land_stays_open_with_a_note(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "stable", "old step here")
    ss.mark_curated(ws, "stable")
    _obs(ws, skill="stable")

    curate_catalog(ws, judge=lambda p: json.dumps({
        "actions": [{"type": "evolve", "name": "stable", "old": "text that is not in the skill",
                     "new": "fixed", "rationale": "obs #1"}],
        "observations": [{"id": 1, "disposition": "applied"}]}))

    rec = _record(ws, 1)
    assert rec["status"] == "OPEN"
    assert "evolve" in rec["attempts"][-1]["note"]


def test_an_applied_claim_with_no_change_and_no_evidence_stays_open(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "stable")
    ss.mark_curated(ws, "stable")
    _obs(ws, skill="stable")

    curate_catalog(ws, judge=lambda p: json.dumps({
        "actions": [], "observations": [{"id": 1, "disposition": "applied"}]}))

    assert _record(ws, 1)["status"] == "OPEN"


def test_evidence_that_is_not_in_the_skill_does_not_count(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "stable", "Step 2: install from PyPI.")
    ss.mark_curated(ws, "stable")
    _obs(ws, skill="stable")

    curate_catalog(ws, judge=lambda p: json.dumps({
        "actions": [], "observations": [{"id": 1, "disposition": "applied",
                                         "evidence": "build from local dist"}]}))

    assert _record(ws, 1)["status"] == "OPEN"


def test_an_edit_waiting_for_approval_is_not_applied(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _mk(ws, "stable", "old step here")
    ss.mark_curated(ws, "stable")
    _obs(ws, skill="stable")
    monkeypatch.setattr(ss, "apply_skill_edit", lambda *a, **k: {
        "error": "edit needs review", "pending_approval": "apr-7"})

    curate_catalog(ws, judge=lambda p: json.dumps({
        "actions": [{"type": "evolve", "name": "stable", "old": "old step here",
                     "new": "new step here", "rationale": "obs #1"}],
        "observations": [{"id": 1, "disposition": "applied"}]}))

    rec = _record(ws, 1)
    assert rec["status"] == "OPEN"
    assert "apr-7" in rec["attempts"][-1]["note"]


def _filed_approval(ws, status: str = "pending") -> str:
    from durin.agent import approval_store

    rec = approval_store.create(ws, kind="skill_edit", summary="edit stable", detail={},
                                payload={}, change_hash="h", session_key=None, context="")
    if status != "pending":
        approval_store.transition(ws, rec["id"], expect=("pending",), to=status)
    return rec["id"]


def _evolve_filed_for_approval(ws, monkeypatch, approval_id: str) -> None:
    monkeypatch.setattr(ss, "apply_skill_edit", lambda *a, **k: {
        "error": "edit needs review", "pending_approval": approval_id})
    curate_catalog(ws, judge=lambda p: json.dumps({
        "actions": [{"type": "evolve", "name": "stable", "old": "old step here",
                     "new": "new step here", "rationale": "obs #1"}],
        "observations": [{"id": 1, "disposition": "applied"}]}))
    monkeypatch.undo()


def test_a_rejected_edit_declines_its_observation(tmp_path, monkeypatch):
    """The person said no: the record is settled, not re-proposed every night."""
    from durin.agent import approval_store

    ws = tmp_path / "ws"
    _mk(ws, "stable", "old step here")
    ss.mark_curated(ws, "stable")
    _obs(ws, skill="stable")
    approval_id = _filed_approval(ws)
    _evolve_filed_for_approval(ws, monkeypatch, approval_id)
    approval_store.transition(ws, approval_id, expect=("pending",), to="rejected")

    curate_catalog(ws, judge=lambda p: '{"actions": []}')

    assert _record(ws, 1)["status"] == "DECLINED"


def test_an_applied_edit_settles_its_observation(tmp_path, monkeypatch):
    from durin.agent import approval_store

    ws = tmp_path / "ws"
    _mk(ws, "stable", "old step here")
    ss.mark_curated(ws, "stable")
    _obs(ws, skill="stable")
    approval_id = _filed_approval(ws)
    _evolve_filed_for_approval(ws, monkeypatch, approval_id)
    approval_store.transition(ws, approval_id, expect=("pending",), to="applied")

    curate_catalog(ws, judge=lambda p: '{"actions": []}')

    assert _record(ws, 1)["status"] == "APPLIED"


def test_an_observation_waiting_for_a_person_is_not_shown_again(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    _mk(ws, "stable", "old step here")
    ss.mark_curated(ws, "stable")
    _obs(ws, skill="stable")
    _evolve_filed_for_approval(ws, monkeypatch, _filed_approval(ws))
    ss.mark_curated(ws, "stable")

    calls = []
    res = curate_catalog(ws, judge=lambda p: calls.append(p) or '{"actions": []}')

    assert res["reviewed"] == 0 and calls == []
    assert _record(ws, 1)["status"] == "OPEN"


def test_a_lesson_already_in_force_settles_its_observation(tmp_path):
    """The judge proposes the principle an "all" record asks for, but it is
    already active: the lesson is in place, so the record is applied."""
    ws = tmp_path / "ws"
    _mk(ws, "stable")
    log_observation(ws, skill="all", kind="improvement", issue="scripts skip dry runs",
                    improvement="every script offers a dry run")
    add_principle(ws, "Every script offers a dry run.")

    curate_catalog(ws, judge=lambda p: json.dumps({
        "actions": [{"type": "principle", "text": "Every script offers a dry run.",
                     "rationale": "obs #1"}],
        "observations": [{"id": 1, "disposition": "applied"}]}))

    assert _record(ws, 1)["status"] == "APPLIED"


def test_an_observation_nothing_can_land_stops_pulling_its_skill_in(tmp_path):
    """After repeated attempts that land nothing, the record waits for a
    person instead of costing a review every night; a new report of it
    lets curation try again."""
    ws = tmp_path / "ws"
    _mk(ws, "stable", "old step here")
    _obs(ws, skill="stable")
    failing = json.dumps({
        "actions": [{"type": "evolve", "name": "stable", "old": "text that is not there",
                     "new": "fixed", "rationale": "obs #1"}],
        "observations": [{"id": 1, "disposition": "applied"}]})
    for _ in range(3):
        ss.mark_curated(ws, "stable")
        curate_catalog(ws, judge=lambda p: failing)
    assert _record(ws, 1).get("stalled_at")

    ss.mark_curated(ws, "stable")
    calls = []
    curate_catalog(ws, judge=lambda p: calls.append(p) or '{"actions": []}')
    assert calls == []

    _obs(ws, skill="stable")
    assert not _record(ws, 1).get("stalled_at")


def test_the_bundled_files_shown_to_the_judge_have_one_budget(tmp_path):
    """Every selected skill may carry scripts; the prompt takes at most one
    budget of them, the skills with open observations first."""
    from durin.agent.skill_curation import _BUNDLES_TOTAL_CHARS

    ws = tmp_path / "ws"
    for i in range(12):
        _mk(ws, f"s{i:02d}")
        scripts = ws / "skills" / f"s{i:02d}" / "scripts"
        scripts.mkdir()
        for j in range(2):
            (scripts / f"run{j}.py").write_text(
                f"# marker s{i:02d}-{j}\n" + "print('x')\n" * 800, encoding="utf-8")
    _obs(ws, skill="s11")

    calls = []
    curate_catalog(ws, judge=lambda p: calls.append(p) or '{"actions": []}')

    shown = calls[0].count("print('x')") * len("print('x')\n")
    assert shown <= _BUNDLES_TOTAL_CHARS
    assert "marker s11-0" in calls[0] and "marker s11-1" in calls[0]


def test_an_evolve_can_fix_a_bundled_script(tmp_path):
    """A fix that belongs in a skill's script must be reachable: the judge sees
    the script and can aim an evolve at it."""
    ws = tmp_path / "ws"
    _mk(ws, "stable")
    script = ws / "skills" / "stable" / "scripts" / "run.py"
    script.parent.mkdir()
    script.write_text("client.get_waiter('query_succeeded')\n", encoding="utf-8")
    ss.mark_curated(ws, "stable")
    _obs(ws, skill="stable", issue="the script uses a waiter the installed botocore lacks")
    prompts = []

    def judge(prompt):
        prompts.append(prompt)
        return json.dumps({
            "actions": [{"type": "evolve", "name": "stable", "file": "scripts/run.py",
                         "old": "client.get_waiter('query_succeeded')",
                         "new": "poll_until_done(client)", "rationale": "obs #1"}],
            "observations": [{"id": 1, "disposition": "applied"}]})

    curate_catalog(ws, judge=judge)

    assert "get_waiter('query_succeeded')" in prompts[0]
    assert script.read_text(encoding="utf-8") == "poll_until_done(client)\n"
    assert _record(ws, 1)["status"] == "APPLIED"


def test_new_prefixed_observations_stay_out_of_curation_prompt(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "changed", "fresh body")       # in delta via change gate
    _obs(ws, skill="new:release-runbook", issue="no skill covers releases")

    calls = []
    curate_catalog(ws, judge=lambda p: calls.append(p) or '{"actions": []}')
    assert "no skill covers releases" not in calls[0]
    # and it stays OPEN for the skill-extract pass
    assert len(open_observations(ws, skill="new:release-runbook")) == 1


def test_all_tagged_observations_reach_judge_when_review_runs(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "changed", "fresh body")
    _obs(ws, skill="all", issue="every skill needs a verification step")

    calls = []
    curate_catalog(ws, judge=lambda p: calls.append(p) or '{"actions": []}')
    assert "every skill needs a verification step" in calls[0]


def test_manual_skills_not_pulled_into_delta_by_observations(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "stable")
    ss.mark_curated(ws, "stable")
    ss.set_mode(ws, "stable", "manual")
    _obs(ws, skill="stable")

    calls = []
    res = curate_catalog(ws, judge=lambda p: calls.append(p) or '{"actions": []}')
    assert res["reviewed"] == 0
    assert calls == []
    assert len(open_observations(ws)) == 1   # stays queued, untouched


# -- cross-cutting principles in curation --------------------------------------


def test_judge_can_promote_a_principle(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "changed", "fresh body")
    _obs(ws, skill="all", issue="every skill needs a verification step", count=2)

    res = curate_catalog(ws, judge=lambda p: json.dumps({
        "actions": [{"type": "principle",
                     "text": "every skill with rules needs a verification step",
                     "rationale": "recurred across skills"}],
        "observations": [{"id": 1, "disposition": "applied"}]}))
    assert res["applied"] == 1
    ps = active_principles(ws)
    assert len(ps) == 1 and "verification" in ps[0]["text"]
    # A cross-skill record is settled by a landed cross-skill change.
    assert open_observations(ws, skill="all") == []


def test_judge_can_retire_a_principle(tmp_path):
    ws = tmp_path / "ws"
    add_principle(ws, "obsolete rule")
    _mk(ws, "changed", "fresh body")

    res = curate_catalog(ws, judge=lambda p: json.dumps({
        "actions": [{"type": "retire_principle", "id": 1}]}))
    assert res["applied"] == 1
    assert active_principles(ws) == []


def test_active_principles_shown_to_judge(tmp_path):
    ws = tmp_path / "ws"
    add_principle(ws, "skills must name their verification command")
    _mk(ws, "changed", "fresh body")

    calls = []
    curate_catalog(ws, judge=lambda p: calls.append(p) or '{"actions": []}')
    assert "skills must name their verification command" in calls[0]


def test_result_reports_open_and_principles_counts(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "changed", "fresh body")
    _obs(ws, skill="changed")              # stays open (judge keeps it)
    add_principle(ws, "some standing rule")

    res = curate_catalog(ws, judge=lambda p: json.dumps({
        "actions": [], "observations": [{"id": 1, "disposition": "keep"}]}))
    assert res["observations"]["open"] == 1
    assert res["principles"] == 1
