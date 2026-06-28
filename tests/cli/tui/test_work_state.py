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


def test_empty_store_render():
    """render_markup() on a brand-new empty store returns '' and is_empty() is True."""
    store = WorkStore()
    assert store.is_empty()
    assert store.render_markup() == ""


def test_subagent_error_phase():
    """A subagent_result event with phase='error' produces a finished item with active_count()==0."""
    store = WorkStore()
    store.ingest({
        "name": "subagent_result", "phase": "error",
        "call_id": "subagent:err1", "label": "validate",
        "error": "validation failed: syntax error",
    })
    assert store.active_count() == 0
    markup = store.render_markup()
    assert "validate" in markup
    assert "✗" in markup  # failed-status glyph
    assert "work-failed" in markup  # failed-status class


def test_workflow_route_label_in_markup():
    """The route_label of a done workflow node appears in the rendered markup."""
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "running",
        "call_id": "workflow:route1",
        "arguments": {"workflow": "decision-tree"},
        "nodes": [
            {"id": "check", "label": "check", "status": "done", "route_label": "pass"},
            {"id": "next", "label": "next", "status": "running", "route_label": None},
        ],
    })
    markup = store.render_markup()
    assert "check" in markup
    assert "pass" in markup  # route_label appears in markup


def test_concurrent_items():
    """Two concurrently-running items (workflow + subagent, different call_ids) yield active_count()==2."""
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "running",
        "call_id": "workflow:w1",
        "arguments": {"workflow": "parallel-task"},
        "nodes": [{"id": "step", "label": "step", "status": "running"}],
    })
    store.ingest({
        "name": "subagent_result", "phase": "running",
        "call_id": "subagent:s1", "label": "research",
        "progress": {"iteration": 1, "tool": "search"},
    })
    assert store.active_count() == 2
    markup = store.render_markup()
    assert "parallel-task" in markup
    assert "research" in markup
