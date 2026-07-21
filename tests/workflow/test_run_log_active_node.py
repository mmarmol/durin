from durin.workflow import run_log


def test_mark_node_started_records_the_active_node(tmp_path):
    run_log.start_run(tmp_path, "wf", "r1", root_session_key=None, started_at=100.0)
    run_log.mark_node_started(tmp_path, "wf", "r1", node_id="scan", label="Scan", started_at=140.0)

    rec = run_log.read_manifest(tmp_path, "wf", "r1")
    assert rec["active_node"] == {"node_id": "scan", "label": "Scan", "started_at": 140.0}
    # The rest of the running manifest survives the rewrite.
    assert rec["status"] == "running"
    assert rec["started_at"] == 100.0


def test_mark_node_started_is_a_noop_without_a_manifest(tmp_path):
    # A nested/headless path may not have written one; must not raise or create a file.
    run_log.mark_node_started(tmp_path, "wf", "missing", node_id="a", label="A", started_at=1.0)
    assert run_log.read_manifest(tmp_path, "wf", "missing") is None


def test_update_run_clears_the_active_node(tmp_path):
    from types import SimpleNamespace

    run_log.start_run(tmp_path, "wf", "r2", root_session_key=None, started_at=100.0)
    run_log.mark_node_started(tmp_path, "wf", "r2", node_id="scan", label="Scan", started_at=140.0)
    result = SimpleNamespace(runs=[])
    run_log.update_run(tmp_path, "wf", "r2", result)

    # The node finished; leaving it marked would pin a finished node as "running".
    assert run_log.read_manifest(tmp_path, "wf", "r2").get("active_node") is None
