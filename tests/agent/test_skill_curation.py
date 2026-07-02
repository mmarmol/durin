import re

from durin.agent import skills_store as ss
from durin.agent.skill_curation import curate_catalog


def _strip_surface_fields(text):
    """Remove name:/description: frontmatter lines, simulating a legacy skill."""
    return re.sub(r"^(name|description):.*\n", "", text, flags=re.MULTILINE)


def _mk(ws, name, body="body"):
    d = ws / "skills" / name; d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\nmetadata:\n  durin:\n    mode: auto\n"
        f"    provenance:\n      source: dream\n---\n{body}\n", encoding="utf-8")


def test_curate_reviews_only_the_changed_delta(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "changed", "new body")        # no cursor → in delta
    _mk(ws, "stable")
    ss.mark_curated(ws, "stable")          # body unchanged since → NOT in delta

    calls = []
    def fake_judge(prompt):
        calls.append(prompt); return '{"actions": []}'

    res = curate_catalog(ws, judge=fake_judge)
    assert res["reviewed"] == 1
    assert "changed" in calls[0] and "stable" not in calls[0]


def test_curate_surfaces_recent_user_edits(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "demo", "body")
    ss.mark_curated(ws, "demo")
    # A hand edit by the user after curation: dream must be told about it.
    ss.save_skill_file(ws, "demo", "SKILL.md",
                       "---\nname: demo\nmetadata:\n  durin:\n    mode: auto\n---\nuser body\n",
                       rationale="edited SKILL.md via web",
                       attribution=ss.Attribution(actor="user"))

    calls = []
    curate_catalog(ws, judge=lambda p: calls.append(p) or '{"actions": [], "observations": []}')
    assert calls, "user edit should pull the skill into the delta"
    assert "edited SKILL.md via web" in calls[0]  # surfaced in the user-edits section


def test_curate_fuses_when_judge_says_overlap(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "git-a", "git rebase steps")
    _mk(ws, "git-b", "git rebase steps too")

    def fake_judge(prompt):
        return ('{"actions": [{"type": "fuse", "target": "git-flow", '
                '"sources": ["git-a", "git-b"], "content": "# Git flow\\nmerged\\n", '
                '"rationale": "same steps"}]}')

    res = curate_catalog(ws, judge=fake_judge)
    assert res["applied"] == 1
    assert (ws / "skills" / "git-flow" / "SKILL.md").exists()
    assert not (ws / "skills" / "git-a").exists()


def test_curate_budget_caps_delta_and_defers_rest(tmp_path):
    ws = tmp_path / "ws"
    for n in ("a", "b", "c"):
        _mk(ws, n)
    res = curate_catalog(ws, judge=lambda p: '{"actions": []}', budget=2)
    assert res["reviewed"] == 2
    assert res["deferred"] == 1


def test_curate_noop_on_empty_delta(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "solo")
    ss.mark_curated(ws, "solo")            # unchanged → empty delta
    calls = []
    res = curate_catalog(ws / ".", judge=lambda p: calls.append(p) or '{"actions": []}')
    # NOTE: ws has one stable skill → delta empty → judge never called
    assert res == {"reviewed": 0, "applied": 0, "deferred": 0, "backfilled": 0, "observations": {"applied": 0, "declined": 0, "kept": 0, "open": 0}, "principles": 0}
    assert calls == []


def test_curate_excludes_pristine_builtins(tmp_path):
    # No workspace skills created → with real shipped builtins present,
    # the delta must still be empty (builtins are source="builtin", excluded).
    ws = tmp_path / "ws"; ws.mkdir()
    calls = []
    res = curate_catalog(ws, judge=lambda p: calls.append(p) or '{"actions": []}')
    assert res == {"reviewed": 0, "applied": 0, "deferred": 0, "backfilled": 0, "observations": {"applied": 0, "declined": 0, "kept": 0, "open": 0}, "principles": 0}
    assert calls == []


def test_curate_skips_fuse_with_out_of_scope_source(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "git-a", "body a")  # the only reviewed skill
    def judge(prompt):
        # judge names a source ("ghost") that was never in the catalog
        return ('{"actions": [{"type": "fuse", "target": "x", '
                '"sources": ["git-a", "ghost"], "content": "merged", "rationale": "r"}]}')
    res = curate_catalog(ws, judge=judge)
    assert res["applied"] == 0
    assert (ws / "skills" / "git-a").exists()   # not fused away
    assert not (ws / "skills" / "x").exists()


def test_curate_skips_evolve_of_out_of_scope_skill(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "git-a", "body a")
    def judge(prompt):
        return ('{"actions": [{"type": "evolve", "name": "ghost", '
                '"old": "x", "new": "y", "rationale": "r"}]}')
    res = curate_catalog(ws, judge=judge)
    assert res["applied"] == 0


def test_curate_retires_obsolete_skill(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "obsolete", "dead weight, fully superseded")
    def judge(prompt):
        return ('{"actions": [{"type": "retire", "name": "obsolete", '
                '"rationale": "fully superseded; no remaining purpose"}]}')
    res = curate_catalog(ws, judge=judge)
    assert res["applied"] == 1
    assert not (ws / "skills" / "obsolete").exists()


def test_curate_skips_retire_of_out_of_scope_skill(tmp_path):
    ws = tmp_path / "ws"
    _mk(ws, "git-a", "body a")
    def judge(prompt):
        return ('{"actions": [{"type": "retire", "name": "ghost", '
                '"rationale": "r"}]}')
    res = curate_catalog(ws, judge=judge)
    assert res["applied"] == 0
    assert (ws / "skills" / "git-a").exists()


def test_curation_backfills_missing_description(tmp_path):
    body = "# QR Reader\n\nDecode QR codes from images.\n\n## Triggers\n\n- QR image attached\n"
    assert ss.dream_create_skill(tmp_path, "qr-reader", body, "seed").get("ok")
    # simulate a legacy skill: strip the frontmatter surface fields
    md = tmp_path / "skills" / "qr-reader" / "SKILL.md"
    text = md.read_text(encoding="utf-8")
    stripped = _strip_surface_fields(text)
    assert "description:" not in stripped
    md.write_text(stripped, encoding="utf-8")
    out = curate_catalog(tmp_path, judge=lambda p: '{"actions": [], "observations": []}')
    assert out.get("backfilled") == 1
    assert "description:" in md.read_text(encoding="utf-8")


# -- telemetry -----------------------------------------------------------------


def test_curate_emits_action_and_run_events(tmp_path, monkeypatch):
    import durin.agent.tools._telemetry as tel
    events = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    ws = tmp_path / "ws"
    _mk(ws, "demo", "old body")

    def fake_judge(prompt):
        return ('{"actions": [{"type": "evolve", "name": "demo", '
                '"old": "old body", "new": "new body", "rationale": "r"}]}')

    res = curate_catalog(ws, judge=fake_judge)
    assert res["applied"] == 1

    actions = [d for n, d in events if n == "skill.curation_action"]
    assert {"action": "evolve", "skill": "demo", "applied": True} in actions

    runs = [d for n, d in events if n == "skill.curation_run"]
    assert len(runs) == 1
    assert runs[0]["reviewed"] == 1
    assert runs[0]["applied"] == 1
    assert runs[0]["deferred"] == 0
    assert runs[0]["backfilled"] == 0


def test_curate_emits_action_event_for_out_of_scope_evolve(tmp_path, monkeypatch):
    # An out-of-scope judge proposal (skill not in `selected`) must still be
    # visible in the event stream as applied=False, not silently dropped —
    # otherwise judge drift (proposing actions on skills it wasn't shown) is
    # invisible to telemetry.
    import durin.agent.tools._telemetry as tel
    events = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    ws = tmp_path / "ws"
    _mk(ws, "git-a", "body a")

    def fake_judge(prompt):
        return ('{"actions": [{"type": "evolve", "name": "ghost", '
                '"old": "x", "new": "y", "rationale": "r"}]}')

    res = curate_catalog(ws, judge=fake_judge)
    assert res["applied"] == 0

    actions = [d for n, d in events if n == "skill.curation_action"]
    assert {"action": "evolve", "skill": "ghost", "applied": False} in actions


def test_curate_backfill_emits_action_event(tmp_path, monkeypatch):
    import durin.agent.tools._telemetry as tel
    events = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    body = "# QR Reader\n\nDecode QR codes from images.\n\n## Triggers\n\n- QR image attached\n"
    assert ss.dream_create_skill(tmp_path, "qr-reader", body, "seed").get("ok")
    md = tmp_path / "skills" / "qr-reader" / "SKILL.md"
    md.write_text(_strip_surface_fields(md.read_text(encoding="utf-8")), encoding="utf-8")

    curate_catalog(tmp_path, judge=lambda p: '{"actions": [], "observations": []}')

    actions = [d for n, d in events if n == "skill.curation_action"]
    assert {"action": "backfill", "skill": "qr-reader", "applied": True} in actions
    runs = [d for n, d in events if n == "skill.curation_run"]
    assert runs[-1]["backfilled"] == 1


def test_curation_backfills_null_description(tmp_path):
    # A bare `description:` key parses to None (PyYAML), not "" — the naive
    # s["description"].strip() call would crash on this instead of repairing it.
    body = "# QR Reader\n\nDecode QR codes from images.\n\n## Triggers\n\n- QR image attached\n"
    assert ss.dream_create_skill(tmp_path, "qr-reader", body, "seed").get("ok")
    md = tmp_path / "skills" / "qr-reader" / "SKILL.md"
    text = md.read_text(encoding="utf-8")
    nulled = re.sub(r"^description:.*\n", "description:\n", text, flags=re.MULTILINE)
    assert "description:\n" in nulled
    md.write_text(nulled, encoding="utf-8")
    out = curate_catalog(tmp_path, judge=lambda p: '{"actions": [], "observations": []}')
    assert out.get("backfilled") == 1
    assert "description:" in md.read_text(encoding="utf-8")
    repaired = ss._frontmatter_description(md.read_text(encoding="utf-8"))
    assert repaired.strip() != ""


def test_backfill_of_already_curated_skill_reenters_delta(tmp_path):
    # Realistic repair case: the skill was already curated (provenance
    # stamped) and only later loses its description — a pure frontmatter
    # backfill doesn't change the body hash, so it must be pulled into the
    # delta explicitly or the judge would never see it.
    body = "# QR Reader\n\nDecode QR codes from images.\n\n## Triggers\n\n- QR image attached\n"
    assert ss.dream_create_skill(tmp_path, "qr-reader", body, "seed").get("ok")
    ss.mark_curated(tmp_path, "qr-reader")
    md = tmp_path / "skills" / "qr-reader" / "SKILL.md"
    md.write_text(_strip_surface_fields(md.read_text(encoding="utf-8")), encoding="utf-8")
    assert ss.needs_curation(tmp_path, "qr-reader") is False  # body untouched

    calls = []
    out = curate_catalog(tmp_path,
                         judge=lambda p: calls.append(p) or '{"actions": [], "observations": []}')
    assert out.get("backfilled") == 1
    assert out["reviewed"] == 1
    assert calls and "qr-reader" in calls[0]


def _mk_auto(ws, name="parse-guard-skill"):
    body = (f"---\nname: {name}\ndescription: A test skill with triggers.\n---\n"
            f"# {name}\n\nDo the thing step by step.\n")
    assert ss.dream_create_skill(ws, name, body, "seed").get("ok")
    return name


def test_curation_unparseable_judge_skips_stamp_and_emits(tmp_path, monkeypatch):
    """Unparseable judge output must NOT consume the review: no curation stamp
    (the skill re-enters the next delta) and a parse-failure event fires."""
    import durin.agent.tools._telemetry as tel

    events = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    name = _mk_auto(tmp_path)
    res = curate_catalog(tmp_path, judge=lambda p: "sorry, the model returned prose")
    assert res.get("judge_parse_failed") is True
    assert res["applied"] == 0
    assert ss.needs_curation(tmp_path, name)  # NOT stamped — re-enters next run
    failures = [d for n, d in events if n == "memory.dream.parse_failure"]
    assert failures and failures[0]["stage"] == "curation"
    runs = [d for n, d in events if n == "skill.curation_run"]
    assert runs and runs[0]["reviewed"] == 1


def test_curation_valid_empty_judge_still_stamps(tmp_path, monkeypatch):
    """A legitimately empty verdict IS a completed review: stamp proceeds."""
    import durin.agent.tools._telemetry as tel

    events = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    name = _mk_auto(tmp_path, "parse-guard-empty")
    res = curate_catalog(tmp_path, judge=lambda p: '{"actions": [], "observations": []}')
    assert "judge_parse_failed" not in res
    assert not ss.needs_curation(tmp_path, name)  # stamped
    assert not [d for n, d in events if n == "memory.dream.parse_failure"]


def test_curation_non_dict_json_treated_as_unparseable(tmp_path):
    name = _mk_auto(tmp_path, "parse-guard-list")
    res = curate_catalog(tmp_path, judge=lambda p: '["not", "a", "dict"]')
    assert res.get("judge_parse_failed") is True
    assert ss.needs_curation(tmp_path, name)


def test_suggest_manual_unparseable_keeps_cursor(tmp_path, monkeypatch):
    """Manual-suggestions path: unparseable judge output must not advance the
    evaluation cursor, so the skill is re-evaluated next run."""
    import durin.agent.tools._telemetry as tel
    from durin.agent import skill_suggestions as sg
    from durin.agent.skill_curation import suggest_manual_skills

    events = []
    monkeypatch.setattr(tel, "emit_tool_event",
                        lambda name, data: events.append((name, data)))
    name = _mk_auto(tmp_path, "parse-guard-manual")
    assert ss.set_mode(tmp_path, name, "manual")  # returns commit sha
    res = suggest_manual_skills(tmp_path, judge=lambda p: "garbage output")
    assert res.get("judge_parse_failed") is True
    assert sg.needs_suggestion(tmp_path, name)  # cursor NOT advanced
    failures = [d for n, d in events if n == "memory.dream.parse_failure"]
    assert failures and failures[0]["stage"] == "suggestions"


def test_curation_fenced_judge_output_is_recovered(tmp_path):
    """A valid action object wrapped in a markdown fence is a completed review
    (recovered like the dream-pass parsers do), not a parse failure."""
    name = _mk_auto(tmp_path, "parse-guard-fenced")
    fenced = '```json\n{"actions": [], "observations": []}\n```'
    res = curate_catalog(tmp_path, judge=lambda p: fenced)
    assert "judge_parse_failed" not in res
    assert not ss.needs_curation(tmp_path, name)  # stamped
