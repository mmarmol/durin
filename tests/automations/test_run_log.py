# tests/automations/test_run_log.py
import time as _time

import pytest

from durin.automations import run_log as rl

_DEAD_OWNER = {"pid": 2**22 + 54321, "started": "never"}


def _cause(kind="schedule", excerpt="fired", trigger_index=0):
    return {"kind": kind, "excerpt": excerpt, "trigger_index": trigger_index}


def _seed(tmp_path, automation, run_id, status, started_at):
    rl.start_run(tmp_path, automation, run_id, cause=_cause())
    rl.update_run(tmp_path, automation, run_id, status=status, started_at=started_at)


def test_runs_root_path(tmp_path):
    assert rl.runs_root(tmp_path) == tmp_path / "automations-runs"


def test_list_runs_on_missing_automation_dir_is_empty(tmp_path):
    assert rl.list_runs(tmp_path, "never-ran") == []


def test_list_all_runs_on_missing_root_is_empty(tmp_path):
    assert rl.list_all_runs(tmp_path) == []


def test_find_orphans_on_missing_root_is_empty(tmp_path):
    assert rl.find_orphans(tmp_path) == []


def test_start_finalize_read(tmp_path):
    rl.start_run(tmp_path, "certs", "r1", cause=_cause(kind="schedule", excerpt="renew"))
    m = rl.read_run(tmp_path, "certs", "r1")
    assert m["status"] == "running"
    assert m["automation"] == "certs"
    assert m["cause"] == {"kind": "schedule", "excerpt": "renew", "trigger_index": 0}
    assert m["delivery"] is None and m["approval"] is None

    rl.finalize_run(tmp_path, "certs", "r1", status="completed", workflow_run_id="w1", final_route_label="OK")
    m = rl.read_run(tmp_path, "certs", "r1")
    assert m["status"] == "completed"
    assert m["workflow_run_id"] == "w1"
    assert m["final_route_label"] == "OK"
    assert m["finished_at"]


def test_start_run_caps_excerpt_at_500(tmp_path):
    rl.start_run(tmp_path, "a", "r1", cause=_cause(excerpt="x" * 600))
    m = rl.read_run(tmp_path, "a", "r1")
    assert len(m["cause"]["excerpt"]) == 500


def test_start_run_origin_none_default(tmp_path):
    rl.start_run(tmp_path, "a", "r1", cause=_cause())
    assert rl.read_run(tmp_path, "a", "r1")["origin"] is None


def test_start_run_origin_dict_roundtrips(tmp_path):
    origin = {"channel": "email", "sender": "user@example.com", "thread": "xyz"}
    rl.start_run(tmp_path, "a", "r1", cause=_cause(), origin=origin)
    assert rl.read_run(tmp_path, "a", "r1")["origin"] == origin


def test_active_runs_includes_running_and_paused(tmp_path):
    rl.start_run(tmp_path, "a", "r1", cause=_cause())
    rl.start_run(tmp_path, "a", "r2", cause=_cause())
    rl.finalize_run(tmp_path, "a", "r2", status="paused")
    rl.start_run(tmp_path, "a", "r3", cause=_cause())
    rl.finalize_run(tmp_path, "a", "r3", status="achieved")

    assert {m["run_id"] for m in rl.active_runs(tmp_path, "a")} == {"r1", "r2"}
    assert len(rl.list_runs(tmp_path, "a")) == 3
    assert any(m["automation"] == "a" for m in rl.list_all_runs(tmp_path))


def test_interrupted_is_not_active(tmp_path):
    rl.start_run(tmp_path, "l1", "r0", cause=_cause())
    rl.finalize_run(tmp_path, "l1", "r0", status="interrupted")
    assert rl.active_runs(tmp_path, "l1") == []


def test_record_delivery_merges_into_run_file(tmp_path):
    rl.start_run(tmp_path, "a", "r1", cause=_cause())
    rl.record_delivery(tmp_path, "a", "r1", channel="email", to="ops@x.test", result="delivered", at_ms=123)
    rec = rl.read_run(tmp_path, "a", "r1")
    assert rec["delivery"] == {"channel": "email", "to": "ops@x.test", "result": "delivered", "at_ms": 123}
    # untouched
    assert rec["status"] == "running"


def test_record_approval_merges_into_run_file(tmp_path):
    rl.start_run(tmp_path, "a", "r1", cause=_cause())
    rl.record_approval(tmp_path, "a", "r1", action="approved", by="marcelo", at_ms=456)
    rec = rl.read_run(tmp_path, "a", "r1")
    assert rec["approval"] == {"action": "approved", "by": "marcelo", "at_ms": 456}


def test_record_delivery_after_finalize_preserves_other_fields(tmp_path):
    rl.start_run(tmp_path, "a", "r1", cause=_cause())
    rl.finalize_run(tmp_path, "a", "r1", status="completed", final_route_label="DONE")
    rl.record_delivery(tmp_path, "a", "r1", channel="slack", to="#ops", result="silenced", at_ms=999)
    rec = rl.read_run(tmp_path, "a", "r1")
    assert rec["status"] == "completed"
    assert rec["final_route_label"] == "DONE"
    assert rec["delivery"] == {"channel": "slack", "to": "#ops", "result": "silenced", "at_ms": 999}


def test_finalize_detail_none_keeps_prior_value(tmp_path):
    rl.start_run(tmp_path, "a", "r1", cause=_cause())
    rl.finalize_run(tmp_path, "a", "r1", status="failed", detail="boom")
    rl.finalize_run(tmp_path, "a", "r1", status="failed")
    assert rl.read_run(tmp_path, "a", "r1")["detail"] == "boom"


def test_finalize_detail_empty_string_clears_prior_value(tmp_path):
    rl.start_run(tmp_path, "a", "r1", cause=_cause())
    rl.finalize_run(tmp_path, "a", "r1", status="failed", detail="boom")
    rl.finalize_run(tmp_path, "a", "r1", status="failed", detail="")
    assert rl.read_run(tmp_path, "a", "r1")["detail"] == ""


def test_update_run_on_missing_file_keeps_required_keys(tmp_path):
    m = rl.update_run(tmp_path, "x", "ghost", status="running")
    assert m["schema"] == rl.SCHEMA
    assert m["run_id"] == "ghost"
    assert m["automation"] == "x"
    assert m["status"] == "running"
    m2 = rl.read_run(tmp_path, "x", "ghost")
    assert m2 == m


def test_consecutive_unachieved_stops_at_achieved(tmp_path):
    for i, status in enumerate(["achieved", "completed", "failed"]):
        rl.start_run(tmp_path, "a", f"r{i}", cause=_cause())
        rl.finalize_run(tmp_path, "a", f"r{i}", status=status)
    assert rl.consecutive_unachieved(tmp_path, "a") == 2


def test_achieved_still_breaks_the_streak(tmp_path):
    for i, status in enumerate(["failed", "achieved", "failed"]):
        rl.start_run(tmp_path, "a", f"r{i}", cause=_cause())
        rl.finalize_run(tmp_path, "a", f"r{i}", status=status)
    assert rl.consecutive_unachieved(tmp_path, "a") == 1


@pytest.mark.parametrize("transparent_status", ["interrupted", "rejected", "paused"])
def test_transparent_statuses_do_not_break_the_streak(tmp_path, transparent_status):
    """A restart, a rejection, or a pause is not evidence the automation is
    working: failures on either side of a transparent run still count as
    consecutive towards the unachieved streak."""
    for i, status in enumerate(["failed", "failed", transparent_status, "failed"]):
        rl.start_run(tmp_path, "a", f"r{i}", cause=_cause())
        rl.finalize_run(tmp_path, "a", f"r{i}", status=status)
    assert rl.consecutive_unachieved(tmp_path, "a") == 3


def test_unknown_status_breaks_the_streak(tmp_path):
    """A defensive default: a status outside the known vocabulary halts the
    streak instead of silently mis-counting it."""
    rl.start_run(tmp_path, "a", "r0", cause=_cause())
    rl.finalize_run(tmp_path, "a", "r0", status="failed")
    rl.start_run(tmp_path, "a", "r1", cause=_cause())
    rl.update_run(tmp_path, "a", "r1", status="some_legacy_status")
    assert rl.consecutive_unachieved(tmp_path, "a") == 0


def test_prune_keeps_paused(tmp_path):
    for i in range(5):
        rl.start_run(tmp_path, "a", f"r{i}", cause=_cause())
        rl.finalize_run(tmp_path, "a", f"r{i}", status="paused" if i == 0 else "completed")
    rl.prune_runs(tmp_path, "a", keep=2)
    left = {m["run_id"] for m in rl.list_runs(tmp_path, "a", limit=50)}
    assert "r0" in left and len(left) == 3  # 2 kept + the paused one


def test_prune_does_not_keep_achieved_beyond_the_limit(tmp_path):
    for i in range(5):
        rl.start_run(tmp_path, "a", f"r{i}", cause=_cause())
        rl.finalize_run(tmp_path, "a", f"r{i}", status="achieved")
    rl.prune_runs(tmp_path, "a", keep=2)
    assert len(rl.list_runs(tmp_path, "a", limit=50)) == 2


# --- list_runs / list_all_runs exempt paused runs from the cap (F8) ---------


def test_list_runs_exempts_a_paused_run_from_the_limit(tmp_path):
    """A paused run is an actionable resume point — the only status
    AutomationsRuntime._park ever sets — and must never drop off the listing
    just because enough newer terminal runs piled up ahead of it, mirroring
    durin.workflow.run_log.list_all_runs's identical needs_input exemption."""
    _seed(tmp_path, "a", "old-paused", "paused", started_at=0.0)
    for i in range(10):
        _seed(tmp_path, "a", f"r{i}", "completed", started_at=float(i + 1))

    listed = rl.list_runs(tmp_path, "a", limit=5)

    assert "old-paused" in {m["run_id"] for m in listed}
    assert len(listed) == 6  # 5 kept terminal + the paused one


def test_list_runs_still_caps_terminal_runs_at_the_limit(tmp_path):
    """The exemption is paused-only — an ordinary terminal-run overflow is
    still capped, same as before this fix."""
    for i in range(10):
        _seed(tmp_path, "a", f"r{i}", "completed", started_at=float(i))

    assert len(rl.list_runs(tmp_path, "a", limit=5)) == 5


def test_list_all_runs_exempts_a_paused_run_from_the_global_cap(tmp_path):
    """Same exemption applied to the global cross-automation feed: 120
    terminal runs plus one old paused run must never push the paused run out
    of the default limit=100 window."""
    _seed(tmp_path, "a", "old-paused", "paused", started_at=0.0)
    for i in range(120):
        _seed(tmp_path, "a", f"r{i}", "completed", started_at=float(i + 1))

    listed = rl.list_all_runs(tmp_path)

    assert "old-paused" in {m["run_id"] for m in listed}
    assert len(listed) == 101  # 100 kept terminal + the paused one


def test_list_all_runs_exempts_paused_across_different_automations(tmp_path):
    """The global feed's exemption applies across the merged cross-automation
    set, not just within one automation's own runs."""
    _seed(tmp_path, "a", "a-paused", "paused", started_at=0.0)
    _seed(tmp_path, "b", "b-paused", "paused", started_at=0.0)
    for i in range(120):
        _seed(tmp_path, "a", f"r{i}", "completed", started_at=float(i + 1))

    listed = rl.list_all_runs(tmp_path)

    ids = {m["run_id"] for m in listed}
    assert {"a-paused", "b-paused"} <= ids


def test_find_orphans_reports_stale_ownerless_run(tmp_path):
    """Legacy ownerless manifest: the age cutoff applies. Detection only —
    the manifest itself is left untouched."""
    rl.start_run(tmp_path, "a", "stale", cause=_cause())
    rl.update_run(tmp_path, "a", "stale", started_at=0.0, owner=None)

    orphans = rl.find_orphans(tmp_path, now=2000.0, max_age_s=100.0)

    assert [o["run_id"] for o in orphans] == ["stale"]
    assert rl.read_run(tmp_path, "a", "stale")["status"] == "running"


def test_find_orphans_reports_dead_owner_regardless_of_age(tmp_path):
    rl.start_run(tmp_path, "a", "ghost", cause=_cause())
    rl.update_run(tmp_path, "a", "ghost", owner=_DEAD_OWNER)  # seconds old

    orphans = rl.find_orphans(tmp_path, now=_time.time())
    assert [o["run_id"] for o in orphans] == ["ghost"]
    assert rl.read_run(tmp_path, "a", "ghost")["status"] == "running"


def test_find_orphans_never_reports_a_live_owner(tmp_path):
    rl.start_run(tmp_path, "a", "mine", cause=_cause())
    rl.update_run(tmp_path, "a", "mine", started_at=0.0)  # ancient but owned

    orphans = rl.find_orphans(tmp_path, now=10**12, max_age_s=1.0)
    assert orphans == []
    assert rl.read_run(tmp_path, "a", "mine")["status"] == "running"


def test_find_orphans_leaves_fresh_run_unreported(tmp_path):
    rl.start_run(tmp_path, "a", "fresh", cause=_cause())
    rl.update_run(tmp_path, "a", "fresh", started_at=1950.0)

    orphans = rl.find_orphans(tmp_path, now=2000.0, max_age_s=100.0)

    assert orphans == []
    assert rl.read_run(tmp_path, "a", "fresh")["status"] == "running"


def test_find_orphans_leaves_paused_unreported(tmp_path):
    rl.start_run(tmp_path, "a", "waiting", cause=_cause())
    rl.update_run(tmp_path, "a", "waiting", started_at=0.0)
    rl.finalize_run(tmp_path, "a", "waiting", status="paused")

    orphans = rl.find_orphans(tmp_path, now=2000.0, max_age_s=100.0)

    assert orphans == []
    assert rl.read_run(tmp_path, "a", "waiting")["status"] == "paused"


def test_find_orphans_reports_without_writing(tmp_path):
    """Detection is separate from the decision: the sweep that can notify and
    relaunch owns the write."""
    rl.start_run(tmp_path, "l1", "r1", cause=_cause())
    rec = rl.read_run(tmp_path, "l1", "r1")
    rec["owner"] = {"pid": 999999, "started": "long ago"}
    rl._write(tmp_path, "l1", "r1", rec)

    orphans = rl.find_orphans(tmp_path)

    assert [o["run_id"] for o in orphans] == ["r1"]
    assert rl.read_run(tmp_path, "l1", "r1")["status"] == "running"


def test_find_orphans_ignores_a_live_owner(tmp_path):
    rl.start_run(tmp_path, "l1", "r1", cause=_cause())
    assert rl.find_orphans(tmp_path) == []


def test_sort_is_deterministic_on_started_at_ties(tmp_path):
    """Runs with equal started_at should sort deterministically by run_id
    descending."""
    run_ids = ["r1", "r2", "r3"]
    shared_started_at = 1000.0
    for run_id in run_ids:
        rl.start_run(tmp_path, "a", run_id, cause=_cause())
    for run_id in run_ids:
        rl.update_run(tmp_path, "a", run_id, started_at=shared_started_at)

    order1 = [m["run_id"] for m in rl.list_runs(tmp_path, "a")]
    order2 = [m["run_id"] for m in rl.list_runs(tmp_path, "a")]

    assert order1 == order2 == sorted(run_ids, reverse=True)
