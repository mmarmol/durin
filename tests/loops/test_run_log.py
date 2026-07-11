# tests/loops/test_run_log.py
from durin.loops import run_log as rl


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
