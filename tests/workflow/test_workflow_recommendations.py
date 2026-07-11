"""Tests for the workflow recommendation queue."""

from durin.workflow import workflow_recommendations as wr


def _log(tmp_path, **kw):
    base = dict(target_id="g", field="criteria", current="old", proposed="new", reason="why")
    base.update(kw)
    return wr.log_recommendation(tmp_path, "wf", **base)


def test_distinct_recommendations_accumulate(tmp_path):
    _log(tmp_path, target_id="g", proposed="tighten X")
    _log(tmp_path, target_id="n", field="prompt", proposed="clarify Y")
    assert len(wr.open_recommendations(tmp_path, "wf")) == 2


def test_duplicate_recommendation_bumps_count_not_rows(tmp_path):
    _log(tmp_path, proposed="tighten the check", run_ids=["r1"])
    _log(tmp_path, proposed="tighten   the   CHECK", run_ids=["r2"])   # same after normalization
    recs = wr.open_recommendations(tmp_path, "wf")
    assert len(recs) == 1
    assert recs[0]["count"] == 2
    assert set(recs[0]["run_ids"]) == {"r1", "r2"}        # run-ids merged


def test_recommendation_carries_the_proposal_fields(tmp_path):
    _log(tmp_path, target_id="gate", field="criteria",
         current="x", proposed="y", reason="loops too often")
    rec = wr.open_recommendations(tmp_path, "wf")[0]
    assert rec["target_id"] == "gate" and rec["field"] == "criteria"
    assert rec["proposed"] == "y" and rec["reason"] == "loops too often"
    assert rec["status"] == "open"


def test_no_recommendations_is_empty(tmp_path):
    assert wr.open_recommendations(tmp_path, "nope") == []


def test_apply_recommendation_edits_node_versions_and_marks_applied(tmp_path):
    import json
    from durin.workflow.loader import workflows_dir
    from durin.workflow.version_store import history_for_dream

    d = workflows_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "wf.json").write_text(json.dumps({
        "name": "wf", "start": "a",
        "nodes": [{"id": "a", "kind": "work", "prompt": "old prompt", "next": None}],
    }))
    rid = wr.log_recommendation(tmp_path, "wf", target_id="a", field="prompt",
                                current="old prompt", proposed="new, sharper prompt", reason="a loops")
    res = wr.apply_recommendation(tmp_path, "wf", rid)
    assert res["ok"] and res["target_id"] == "a"

    data = json.loads((d / "wf.json").read_text())
    assert next(n for n in data["nodes"] if n["id"] == "a")["prompt"] == "new, sharper prompt"
    assert wr.open_recommendations(tmp_path, "wf") == []            # no longer open
    assert any("apply recommendation" in h["reason"] for h in history_for_dream(tmp_path, "wf"))


def test_apply_unknown_recommendation_errors(tmp_path):
    from durin.workflow.loader import workflows_dir
    workflows_dir(tmp_path).mkdir(parents=True)
    res = wr.apply_recommendation(tmp_path, "wf", "nope")
    assert not res["ok"] and "no open recommendation" in res["error"]


def test_manual_only_defaults_false_and_absent_on_old_records(tmp_path):
    rid = _log(tmp_path, target_id="a", field="prompt", proposed="x")
    rec = wr.open_recommendations(tmp_path, "wf")[0]
    assert "manual_only" not in rec                       # shape-stable: absent, not False
    assert rec.get("manual_only", False) is False          # readers must treat absence as False
    assert rid


def test_log_recommendation_manual_only_persisted(tmp_path):
    _log(tmp_path, target_id="gate", field="criteria", proposed="tighten", manual_only=True)
    rec = wr.open_recommendations(tmp_path, "wf")[0]
    assert rec["manual_only"] is True


def test_script_file_recommendation_logs_and_dedups(tmp_path):
    rid1 = wr.log_script_file_recommendation(
        tmp_path, "wf", script="fix.sh", current="old body", proposed="new body",
        reason="node crashes", run_ids=["r1"],
    )
    rid2 = wr.log_script_file_recommendation(
        tmp_path, "wf", script="fix.sh", current="old body", proposed="new body",
        reason="node crashes again", run_ids=["r2"],
    )
    assert rid1 == rid2
    recs = wr.open_recommendations(tmp_path, "wf")
    assert len(recs) == 1
    rec = recs[0]
    assert rec["kind"] == "script_file"
    assert rec["script"] == "fix.sh"
    assert rec["proposed"] == "new body"
    assert rec["count"] == 2
    assert set(rec["run_ids"]) == {"r1", "r2"}
    assert "manual_only" not in rec


def test_script_file_recommendation_manual_only(tmp_path):
    wr.log_script_file_recommendation(
        tmp_path, "wf", script="route.py", current="old", proposed="new",
        reason="routing script referenced", manual_only=True,
    )
    rec = wr.open_recommendations(tmp_path, "wf")[0]
    assert rec["manual_only"] is True


def test_apply_script_file_recommendation_writes_file_and_marks_applied(tmp_path):
    from durin.workflow.loader import workflows_dir

    scripts_dir = workflows_dir(tmp_path) / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "fix.sh").write_text("#!/bin/sh\necho old\n")

    rid = wr.log_script_file_recommendation(
        tmp_path, "wf", script="fix.sh", current="#!/bin/sh\necho old\n",
        proposed="#!/bin/sh\necho new\n", reason="node crashes",
    )
    res = wr.apply_recommendation(tmp_path, "wf", rid)
    assert res["ok"] and res["script"] == "fix.sh"

    assert (scripts_dir / "fix.sh").read_text() == "#!/bin/sh\necho new\n"
    assert wr.open_recommendations(tmp_path, "wf") == []

    applied = next(r for r in wr._read(wr._path(tmp_path, "wf")) if r["id"] == rid)
    assert applied["status"] == "applied"
    assert applied["applied_by"] == "user"


def test_apply_script_file_recommendation_creates_missing_scripts_dir(tmp_path):
    from durin.workflow.loader import workflows_dir

    rid = wr.log_script_file_recommendation(
        tmp_path, "wf", script="new_script.sh", current="",
        proposed="#!/bin/sh\necho hi\n", reason="add missing script",
    )
    res = wr.apply_recommendation(tmp_path, "wf", rid)
    assert res["ok"]
    assert (workflows_dir(tmp_path) / "scripts" / "new_script.sh").read_text() == "#!/bin/sh\necho hi\n"


def test_apply_script_file_recommendation_rejects_traversal_name(tmp_path):
    rid = wr.log_script_file_recommendation(
        tmp_path, "wf", script="../evil.sh", current="", proposed="rm -rf /",
        reason="malicious proposal",
    )
    res = wr.apply_recommendation(tmp_path, "wf", rid)
    assert not res["ok"]
    assert "single path segment" in res["error"]
    recs = wr.open_recommendations(tmp_path, "wf")
    assert len(recs) == 1 and recs[0]["id"] == rid          # stays open, nothing written


def test_apply_script_file_recommendation_rejects_backslash_and_nul(tmp_path):
    rid = wr.log_script_file_recommendation(
        tmp_path, "wf", script="a\\b", current="", proposed="x", reason="bad name",
    )
    res = wr.apply_recommendation(tmp_path, "wf", rid)
    assert not res["ok"]


def test_apply_command_recommendation_on_script_node_end_to_end(tmp_path):
    import json
    from durin.workflow.loader import workflows_dir

    d = workflows_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "wf.json").write_text(json.dumps({
        "name": "wf", "start": "a",
        "nodes": [{"id": "a", "kind": "script", "command": "echo old", "next": None}],
    }))
    rid = wr.log_recommendation(tmp_path, "wf", target_id="a", field="command",
                                current="echo old", proposed="echo new", reason="a crashes")
    res = wr.apply_recommendation(tmp_path, "wf", rid)
    assert res["ok"] and res["target_id"] == "a" and res["field"] == "command"

    data = json.loads((d / "wf.json").read_text())
    assert next(n for n in data["nodes"] if n["id"] == "a")["command"] == "echo new"
    assert wr.open_recommendations(tmp_path, "wf") == []


def test_apply_command_recommendation_refuses_on_precheck_failure(tmp_path):
    """apply_recommendation re-runs the pre-apply gate at apply time (not just
    when the dream first proposed the edit): a hand-crafted open recommendation
    whose proposed command has a bash syntax error must be refused, and the
    recommendation must stay open (nothing gets written)."""
    import json
    from durin.workflow.loader import workflows_dir

    d = workflows_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "wf.json").write_text(json.dumps({
        "name": "wf", "start": "a",
        "nodes": [{"id": "a", "kind": "script", "command": "true", "next": None}],
    }))
    rid = wr.log_recommendation(tmp_path, "wf", target_id="a", field="command",
                                current="true", proposed="if [ 1 -eq 1 ]; then echo hi",
                                reason="fix it")
    res = wr.apply_recommendation(tmp_path, "wf", rid)
    assert res["ok"] is False
    assert res["error"].startswith("precheck failed")

    data = json.loads((d / "wf.json").read_text())
    assert next(n for n in data["nodes"] if n["id"] == "a")["command"] == "true"   # untouched
    recs = wr.open_recommendations(tmp_path, "wf")
    assert len(recs) == 1 and recs[0]["id"] == rid   # stays open


def test_apply_command_recommendation_fails_when_node_also_has_script(tmp_path):
    import json
    from durin.workflow.loader import workflows_dir

    d = workflows_dir(tmp_path)
    d.mkdir(parents=True)
    (d / "wf.json").write_text(json.dumps({
        "name": "wf", "start": "a",
        "nodes": [{"id": "a", "kind": "script", "script": "run.sh", "next": None}],
    }))
    rid = wr.log_recommendation(tmp_path, "wf", target_id="a", field="command",
                                current="", proposed="echo new", reason="a crashes")
    res = wr.apply_recommendation(tmp_path, "wf", rid)
    assert not res["ok"] and "error" in res

    data = json.loads((d / "wf.json").read_text())
    assert next(n for n in data["nodes"] if n["id"] == "a").get("command") is None  # unchanged on disk
    recs = wr.open_recommendations(tmp_path, "wf")
    assert len(recs) == 1 and recs[0]["id"] == rid          # rec stays open
