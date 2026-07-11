# tests/loops/test_run_log.py
from durin.loops import run_log as rl


def test_reconcile_running_flips_stale_run_to_error(tmp_path):
    rl.start_run(tmp_path, "a", "stale", source="cron", task="t")
    rl.update_run(tmp_path, "a", "stale", started_at=0.0)

    flipped = rl.reconcile_running(tmp_path, now=2000.0, max_age_s=100.0)

    assert flipped == ["stale"]
    rec = rl.read_run(tmp_path, "a", "stale")
    assert rec["status"] == "error"
    assert rec["ask"] is None


def test_reconcile_running_leaves_fresh_run_untouched(tmp_path):
    rl.start_run(tmp_path, "a", "fresh", source="cron", task="t")
    rl.update_run(tmp_path, "a", "fresh", started_at=1950.0)

    flipped = rl.reconcile_running(tmp_path, now=2000.0, max_age_s=100.0)

    assert flipped == []
    assert rl.read_run(tmp_path, "a", "fresh")["status"] == "running"


def test_reconcile_running_leaves_needs_operator_untouched(tmp_path):
    rl.start_run(tmp_path, "a", "waiting", source="cron", task="t")
    rl.update_run(tmp_path, "a", "waiting", started_at=0.0)
    rl.finalize_run(tmp_path, "a", "waiting", status="needs_operator", ask="approve?")

    flipped = rl.reconcile_running(tmp_path, now=2000.0, max_age_s=100.0)

    assert flipped == []
    assert rl.read_run(tmp_path, "a", "waiting")["status"] == "needs_operator"


def test_start_finalize_read(tmp_path):
    rl.start_run(tmp_path, "certs", "r1", source="cron", task="renew")
    m = rl.read_run(tmp_path, "certs", "r1")
    assert m["status"] == "running" and m["loop"] == "certs" and m["source"] == "cron"
    rl.finalize_run(tmp_path, "certs", "r1", status="done", workflow_run_id="w1", goal_reached=True)
    m = rl.read_run(tmp_path, "certs", "r1")
    assert m["status"] == "done" and m["workflow_run_id"] == "w1" and m["finished_at"]


def test_active_runs_and_lists(tmp_path):
    rl.start_run(tmp_path, "a", "r1", source="manual", task="t")
    rl.start_run(tmp_path, "a", "r2", source="manual", task="t")
    rl.finalize_run(tmp_path, "a", "r2", status="needs_operator", ask="approve?")
    rl.start_run(tmp_path, "a", "r3", source="manual", task="t")
    rl.finalize_run(tmp_path, "a", "r3", status="done", goal_reached=True)
    assert {m["run_id"] for m in rl.active_runs(tmp_path, "a")} == {"r1", "r2"}
    assert len(rl.list_runs(tmp_path, "a")) == 3
    assert any(m["loop"] == "a" for m in rl.list_all_runs(tmp_path))


def test_consecutive_no_goal_stops_at_done(tmp_path):
    for i, status in enumerate(["done", "no_goal", "error", "no_goal"]):
        rl.start_run(tmp_path, "a", f"r{i}", source="cron", task="t")
        rl.finalize_run(tmp_path, "a", f"r{i}", status=status)
    assert rl.consecutive_no_goal(tmp_path, "a") == 3


def test_prune_keeps_needs_operator(tmp_path):
    for i in range(5):
        rl.start_run(tmp_path, "a", f"r{i}", source="cron", task="t")
        rl.finalize_run(tmp_path, "a", f"r{i}", status="needs_operator" if i == 0 else "done")
    rl.prune_runs(tmp_path, "a", keep=2)
    left = {m["run_id"] for m in rl.list_runs(tmp_path, "a", limit=50)}
    assert "r0" in left and len(left) == 3  # 2 kept + the needs_operator one


def test_finalize_ask_none_keeps_prior_ask(tmp_path):
    rl.start_run(tmp_path, "a", "r1", source="cron", task="t")
    rl.finalize_run(tmp_path, "a", "r1", status="needs_operator", ask="approve?")
    rl.finalize_run(tmp_path, "a", "r1", status="running")
    assert rl.read_run(tmp_path, "a", "r1")["ask"] == "approve?"


def test_finalize_ask_empty_string_clears_prior_ask(tmp_path):
    rl.start_run(tmp_path, "a", "r1", source="cron", task="t")
    rl.finalize_run(tmp_path, "a", "r1", status="needs_operator", ask="approve?")
    rl.finalize_run(tmp_path, "a", "r1", status="running", ask="")
    assert rl.read_run(tmp_path, "a", "r1")["ask"] == ""


def test_start_run_seeds_detail_none(tmp_path):
    rec = rl.start_run(tmp_path, "a", "r1", source="cron", task="t")
    assert rec["detail"] is None


def test_finalize_detail_none_keeps_prior_value_stores(tmp_path):
    rl.start_run(tmp_path, "a", "r1", source="cron", task="t")
    rl.finalize_run(tmp_path, "a", "r1", status="error", detail="boom")
    assert rl.read_run(tmp_path, "a", "r1")["detail"] == "boom"
    rl.finalize_run(tmp_path, "a", "r1", status="error")
    assert rl.read_run(tmp_path, "a", "r1")["detail"] == "boom"


def test_update_run_on_missing_file_keeps_required_keys(tmp_path):
    """update_run on a nonexistent run should seed required manifest keys."""
    m = rl.update_run(tmp_path, "x", "ghost", status="running")
    assert m["schema"] == rl.SCHEMA
    assert m["run_id"] == "ghost"
    assert m["loop"] == "x"
    assert m["status"] == "running"
    # Verify it round-trips correctly
    m2 = rl.read_run(tmp_path, "x", "ghost")
    assert m2["schema"] == rl.SCHEMA and m2["run_id"] == "ghost" and m2["loop"] == "x" and m2["status"] == "running"


def test_sort_is_deterministic_on_started_at_ties(tmp_path):
    """Runs with equal started_at should sort deterministically by run_id descending."""
    # Create three runs with the same started_at
    run_ids = ["r1", "r2", "r3"]
    shared_started_at = 1000.0
    for run_id in run_ids:
        rl.start_run(tmp_path, "a", run_id, source="cron", task="t")

    # Rewrite them to share the same started_at
    for run_id in run_ids:
        rl.update_run(tmp_path, "a", run_id, started_at=shared_started_at)

    # Call list_runs twice and verify same order both times
    runs1 = rl.list_runs(tmp_path, "a")
    runs2 = rl.list_runs(tmp_path, "a")

    order1 = [m["run_id"] for m in runs1]
    order2 = [m["run_id"] for m in runs2]

    assert order1 == order2, f"Orders differ: {order1} vs {order2}"

    # Verify order is deterministic (should be sorted by run_id descending for ties)
    expected = sorted(run_ids, reverse=True)
    assert order1 == expected, f"Expected {expected}, got {order1}"


def test_list_all_runs_deterministic_on_ties(tmp_path):
    """list_all_runs across multiple loops with tied started_at should be deterministic by run_id."""
    # Create runs in two different loops with identical started_at
    shared_started_at = 2000.0
    a_run_ids = ["a_r1", "a_r2", "a_r3"]
    b_run_ids = ["b_r1", "b_r2"]

    # Create runs in loop "a"
    for run_id in a_run_ids:
        rl.start_run(tmp_path, "a", run_id, source="cron", task="t")
        rl.update_run(tmp_path, "a", run_id, started_at=shared_started_at)

    # Create runs in loop "b"
    for run_id in b_run_ids:
        rl.start_run(tmp_path, "b", run_id, source="cron", task="t")
        rl.update_run(tmp_path, "b", run_id, started_at=shared_started_at)

    # Call list_all_runs twice and verify same order both times
    all_runs1 = rl.list_all_runs(tmp_path)
    all_runs2 = rl.list_all_runs(tmp_path)

    order1 = [m["run_id"] for m in all_runs1]
    order2 = [m["run_id"] for m in all_runs2]

    assert order1 == order2, f"Orders differ: {order1} vs {order2}"

    # Verify order is deterministic (should be sorted by run_id descending for ties)
    all_run_ids = a_run_ids + b_run_ids
    expected = sorted(all_run_ids, reverse=True)
    assert order1 == expected, f"Expected {expected}, got {order1}"


def test_start_run_origin_none_default(tmp_path):
    """start_run without origin parameter should store None."""
    rl.start_run(tmp_path, "a", "r1", source="cron", task="t")
    m = rl.read_run(tmp_path, "a", "r1")
    assert m["origin"] is None


def test_start_run_origin_dict_roundtrips(tmp_path):
    """start_run with origin dict should roundtrip correctly."""
    origin = {"channel": "email", "sender": "user@example.com", "chat_id": "123", "thread": "xyz", "subject": "test"}
    rl.start_run(tmp_path, "a", "r1", source="mail", task="t", origin=origin)
    m = rl.read_run(tmp_path, "a", "r1")
    assert m["origin"] == origin


def test_waiting_info_is_active(tmp_path):
    """waiting_info status should be included in active_runs."""
    rl.start_run(tmp_path, "a", "r1", source="manual", task="t")
    rl.start_run(tmp_path, "a", "r2", source="manual", task="t")
    rl.finalize_run(tmp_path, "a", "r2", status="waiting_info")
    rl.start_run(tmp_path, "a", "r3", source="manual", task="t")
    rl.finalize_run(tmp_path, "a", "r3", status="done")
    active = {m["run_id"] for m in rl.active_runs(tmp_path, "a")}
    assert active == {"r1", "r2"}


def test_prune_keeps_waiting_info(tmp_path):
    """prune_runs should never prune waiting_info runs."""
    for i in range(5):
        rl.start_run(tmp_path, "a", f"r{i}", source="cron", task="t")
        status = "waiting_info" if i == 0 else "done"
        rl.finalize_run(tmp_path, "a", f"r{i}", status=status)
    rl.prune_runs(tmp_path, "a", keep=2)
    left = {m["run_id"] for m in rl.list_runs(tmp_path, "a", limit=50)}
    assert "r0" in left and len(left) == 3  # 2 kept + the waiting_info one


def test_consecutive_no_goal_skips_waiting_info(tmp_path):
    """consecutive_no_goal should skip waiting_info (treat it like needs_operator)."""
    for i, status in enumerate(["no_goal", "error", "no_goal", "waiting_info"]):
        rl.start_run(tmp_path, "a", f"r{i}", source="cron", task="t")
        rl.finalize_run(tmp_path, "a", f"r{i}", status=status)
    assert rl.consecutive_no_goal(tmp_path, "a") == 3
