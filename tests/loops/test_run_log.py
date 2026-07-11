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
