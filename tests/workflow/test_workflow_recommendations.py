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
