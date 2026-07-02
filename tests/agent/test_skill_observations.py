"""Observation store: live skill-feedback queue consumed by curation.

Pure tmp_path tests over durin.agent.skill_observations — no provider, no LLM.
"""
from durin.agent.skill_observations import (
    PRINCIPLES_CAP,
    active_principles,
    add_principle,
    apply_dispositions,
    archive_resolved,
    declined_observations,
    log_observation,
    open_observations,
    retire_principle,
)


def _log(ws, **kw):
    base = {"skill": "deploy-gateway", "kind": "correction",
            "issue": "user corrected the wheel build step",
            "improvement": "build from local dist, not PyPI"}
    base.update(kw)
    return log_observation(ws, **base)


def test_log_creates_open_record_with_id_and_count(tmp_path):
    ws = tmp_path / "ws"
    res = _log(ws)
    assert res["ok"] is True
    assert res["id"] == 1
    assert res["count"] == 1
    recs = open_observations(ws)
    assert len(recs) == 1
    r = recs[0]
    assert r["skill"] == "deploy-gateway"
    assert r["kind"] == "correction"
    assert r["status"] == "OPEN"
    assert r["count"] == 1
    assert r["first_seen"] == r["last_seen"]


def test_log_commits_to_skills_gitstore(tmp_path):
    ws = tmp_path / "ws"
    res = _log(ws)
    assert res["commit"]
    assert (ws / "skills" / ".observations.jsonl").exists()


def test_log_rejects_bad_kind(tmp_path):
    ws = tmp_path / "ws"
    res = _log(ws, kind="vibe")
    assert "error" in res
    assert open_observations(ws) == []


def test_log_rejects_unsafe_skill_name(tmp_path):
    ws = tmp_path / "ws"
    assert "error" in _log(ws, skill="../evil")
    assert "error" in _log(ws, skill="")


def test_log_accepts_all_and_new_prefixed_skill(tmp_path):
    ws = tmp_path / "ws"
    assert _log(ws, skill="all")["ok"] is True
    assert _log(ws, skill="new:release-runbook",
                issue="no skill covers releases")["ok"] is True


def test_log_rejects_empty_issue(tmp_path):
    ws = tmp_path / "ws"
    res = _log(ws, issue="  ")
    assert "error" in res


def test_duplicate_issue_bumps_count_instead_of_new_record(tmp_path):
    ws = tmp_path / "ws"
    _log(ws, session="s1")
    res = _log(ws, issue="User corrected the wheel build  step", session="s2")
    assert res["ok"] is True
    assert res["id"] == 1
    assert res["count"] == 2
    recs = open_observations(ws)
    assert len(recs) == 1
    assert recs[0]["count"] == 2
    assert recs[0]["sessions"] == ["s1", "s2"]


def test_different_issue_gets_new_id(tmp_path):
    ws = tmp_path / "ws"
    _log(ws)
    res = _log(ws, issue="gateway port doc was wrong")
    assert res["id"] == 2
    assert len(open_observations(ws)) == 2


def test_dedup_is_per_skill(tmp_path):
    ws = tmp_path / "ws"
    _log(ws, skill="skill-a")
    res = _log(ws, skill="skill-b")
    assert res["id"] == 2


def test_open_observations_filters_by_skill(tmp_path):
    ws = tmp_path / "ws"
    _log(ws, skill="skill-a")
    _log(ws, skill="skill-b", issue="other thing")
    only_a = open_observations(ws, skill="skill-a")
    assert [r["skill"] for r in only_a] == ["skill-a"]


def test_open_observations_empty_when_no_store(tmp_path):
    assert open_observations(tmp_path / "ws") == []


# -- dispositions + archive (consumed by the curation pass) -------------------


def test_apply_dispositions_transitions_states(tmp_path):
    ws = tmp_path / "ws"
    _log(ws, issue="a")
    _log(ws, issue="b")
    _log(ws, issue="c")
    res = apply_dispositions(ws, [
        {"id": 1, "disposition": "applied"},
        {"id": 2, "disposition": "declined"},
        {"id": 3, "disposition": "keep"},
    ])
    assert res["applied"] == 1 and res["declined"] == 1 and res["kept"] == 1
    assert res["commit"]
    assert [r["id"] for r in open_observations(ws)] == [3]
    assert [r["id"] for r in declined_observations(ws)] == [2]


def test_apply_dispositions_ignores_unknown_ids(tmp_path):
    ws = tmp_path / "ws"
    _log(ws)
    res = apply_dispositions(ws, [{"id": 99, "disposition": "applied"}])
    assert res["applied"] == 0
    assert len(open_observations(ws)) == 1


def test_archive_moves_applied_keeps_declined_and_open(tmp_path):
    ws = tmp_path / "ws"
    _log(ws, issue="a")
    _log(ws, issue="b")
    _log(ws, issue="c")
    apply_dispositions(ws, [{"id": 1, "disposition": "applied"},
                            {"id": 2, "disposition": "declined"}])
    moved = archive_resolved(ws)
    assert moved == 1
    assert (ws / "skills" / ".observations.archive.jsonl").exists()
    active_ids = {r["id"] for r in open_observations(ws)} | {
        r["id"] for r in declined_observations(ws)}
    assert active_ids == {2, 3}


def test_ids_stay_monotonic_after_archive(tmp_path):
    ws = tmp_path / "ws"
    _log(ws, issue="a")
    apply_dispositions(ws, [{"id": 1, "disposition": "applied"}])
    archive_resolved(ws)
    res = _log(ws, issue="something new")
    assert res["id"] == 2


def test_archive_noop_when_nothing_resolved(tmp_path):
    ws = tmp_path / "ws"
    _log(ws)
    assert archive_resolved(ws) == 0


# -- cross-cutting principles --------------------------------------------------


def test_add_principle_assigns_id_and_commits(tmp_path):
    ws = tmp_path / "ws"
    res = add_principle(ws, "every skill with rules needs an enforcement step")
    assert res["ok"] is True and res["id"] == 1 and res["commit"]
    ps = active_principles(ws)
    assert len(ps) == 1 and ps[0]["text"].startswith("every skill")


def test_add_principle_rejects_empty_and_duplicate(tmp_path):
    ws = tmp_path / "ws"
    assert "error" in add_principle(ws, "  ")
    add_principle(ws, "keep skills concise")
    assert "error" in add_principle(ws, "Keep  skills concise")


def test_add_principle_refuses_beyond_cap(tmp_path):
    ws = tmp_path / "ws"
    for i in range(PRINCIPLES_CAP):
        assert add_principle(ws, f"principle number {i}")["ok"] is True
    res = add_principle(ws, "one too many")
    assert "error" in res
    assert len(active_principles(ws)) == PRINCIPLES_CAP


def test_retire_principle_frees_a_slot(tmp_path):
    ws = tmp_path / "ws"
    add_principle(ws, "first")
    add_principle(ws, "second")
    assert retire_principle(ws, 1)["ok"] is True
    assert [p["id"] for p in active_principles(ws)] == [2]
    assert add_principle(ws, "third")["id"] == 3


def test_retire_unknown_principle_errors(tmp_path):
    ws = tmp_path / "ws"
    assert "error" in retire_principle(ws, 7)


def test_paraphrased_issue_still_dedups(tmp_path):
    # Live finding 2026-06-10: the LLM rephrases the same issue each time
    # ("Step 2 says X" vs "Step 2 still says X. This is the SECOND time...").
    # Containment missed it; word-overlap similarity must catch it.
    ws = tmp_path / "ws"
    _log(ws, issue='Step 2 says "Install from PyPI with pipx install durin-agent" '
                   "but the gateway is NEVER installed from PyPI - it must be "
                   "installed from the local wheel in dist/")
    res = _log(ws, issue='Step 2 still says "Install from PyPI with pipx install '
                         "durin-agent\". This is the SECOND time the user corrects "
                         "this - install from the local wheel in dist/ instead")
    assert res["id"] == 1
    assert res["count"] == 2


def test_unrelated_issue_same_skill_does_not_dedup(tmp_path):
    ws = tmp_path / "ws"
    _log(ws, issue="step 2 installs from the wrong source registry entirely")
    res = _log(ws, issue="the restart step forgets to check the pid file first")
    assert res["id"] == 2


# -- telemetry -----------------------------------------------------------------


def test_log_observation_emits_event(tmp_path, monkeypatch):
    import durin.agent.tools._telemetry as tel
    events = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    ws = tmp_path / "ws"
    _log(ws)
    logged = [d for n, d in events if n == "skill.observation_logged"]
    assert len(logged) == 1
    assert logged[0] == {"skill": "deploy-gateway", "kind": "correction",
                          "dedup_bumped": False, "count": 1}


def test_log_observation_dedup_bump_emits_event(tmp_path, monkeypatch):
    import durin.agent.tools._telemetry as tel
    events = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    ws = tmp_path / "ws"
    _log(ws, session="s1")
    events.clear()
    _log(ws, issue="User corrected the wheel build  step", session="s2")
    logged = [d for n, d in events if n == "skill.observation_logged"]
    assert len(logged) == 1
    assert logged[0] == {"skill": "deploy-gateway", "kind": "correction",
                          "dedup_bumped": True, "count": 2}
