from durin.cli.tui.widgets.work_state import WorkStore


def test_workflow_running_with_parallel_branches_then_finished():
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "running",
        "call_id": "workflow:r1",
        "arguments": {"workflow": "review-changes", "task": "review the diff"},
        "nodes": [
            {"id": "scan", "label": "scan", "status": "done", "route_label": "pass"},
            {"id": "fix", "label": "fix", "status": "running", "route_label": None,
             "branches": [
                 {"id": "v_auth", "label": "verify:auth", "status": "running"},
                 {"id": "v_api", "label": "verify:api", "status": "done"},
             ]},
        ],
    })
    assert store.active_count() == 1
    assert not store.is_empty()
    markup = store.render_markup()
    assert "review-changes" in markup
    assert "scan" in markup and "fix" in markup
    assert "verify:auth" in markup  # nested parallel branch is rendered

    store.ingest({
        "name": "workflow_progress", "phase": "end",
        "call_id": "workflow:r1",
        "arguments": {"workflow": "review-changes"},
        "nodes": [{"id": "fix", "label": "fix", "status": "done", "route_label": None}],
    })
    assert store.active_count() == 0


def test_subagent_running_and_result():
    store = WorkStore()
    store.ingest({
        "name": "subagent_result", "phase": "running",
        "call_id": "subagent:t1", "label": "explore",
        "progress": {"iteration": 2, "tool": "grep"},
    })
    assert store.active_count() == 1
    assert "explore" in store.render_markup()
    store.ingest({
        "name": "subagent_result", "phase": "end",
        "call_id": "subagent:t1", "label": "explore",
        "result": "found 3 files",
    })
    assert store.active_count() == 0


def test_unknown_event_ignored():
    store = WorkStore()
    store.ingest({"name": "edit_file", "phase": "end", "call_id": "x"})
    assert store.is_empty()
