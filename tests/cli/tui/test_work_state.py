import re

from rich.markup import render as rich_render

from durin.cli.tui.widgets.work_state import WorkStore


def test_running_node_glyph_advances_with_spin_frame():
    store = WorkStore()
    store.ingest({
        "name": "subagent_result", "phase": "running",
        "call_id": "subagent:t1", "label": "explore",
    })
    frame0 = store.render_markup(0)
    frame1 = store.render_markup(1)
    # The running spinner glyph differs between consecutive frames.
    assert frame0 != frame1
    assert "explore" in frame0 and "explore" in frame1


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


def test_workflow_needs_input_is_waiting_not_finished():
    """A terminal frame with status=needs_input renders as an active 'waiting'
    item (glyph ?, its own style, first question line) — not under Finished."""
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "running",
        "call_id": "workflow:n1",
        "arguments": {"workflow": "triage"},
        "nodes": [{"id": "ask", "label": "ask", "status": "running"}],
    })
    store.ingest({
        "name": "workflow_progress", "phase": "end",
        "call_id": "workflow:n1",
        "status": "needs_input",
        "detail": "Which environment: staging or prod?",
        "arguments": {"workflow": "triage"},
        "nodes": [{"id": "ask", "label": "ask", "status": "needs_input"}],
    })
    # Paused run is waiting on the user: no spinner animation needed…
    assert store.active_count() == 0
    markup = store.render_markup()
    # …but it stays in the active section, styled and explained.
    assert "Finished" not in markup
    assert "1 waiting" in markup
    assert "work-needs-input" in markup
    assert "? triage" in markup
    assert "waiting for your reply in chat" in markup
    assert "Which environment" in markup


def test_workflow_needs_input_detail_escapes_markup():
    """LLM question text may contain literal brackets — they must not be parsed
    as Rich markup tags when rendered in the sidebar."""
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "end",
        "call_id": "workflow:n2",
        "status": "needs_input",
        "detail": "Pick one of [staging] or [prod]",
        "arguments": {"workflow": "deploy"},
        "nodes": [],
    })
    markup = store.render_markup()
    assert r"\[staging]" in markup


def test_workflow_end_without_status_stays_done():
    """Back-compat: terminal frames from emitters that don't send a run status
    keep the plain end→done mapping."""
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "end",
        "call_id": "workflow:old1",
        "arguments": {"workflow": "legacy"},
        "nodes": [],
    })
    markup = store.render_markup()
    assert "Finished" in markup
    assert "work-done" in markup


def test_workflow_end_non_completed_status_is_failed():
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "end",
        "call_id": "workflow:x1",
        "status": "exhausted",
        "arguments": {"workflow": "flaky"},
        "nodes": [],
    })
    assert store.active_count() == 0
    markup = store.render_markup()
    assert "Finished" in markup
    assert "work-failed" in markup


def test_running_node_renders_round_and_activity():
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "running", "call_id": "workflow:r1",
        "arguments": {"workflow": "wf"},
        "nodes": [{
            "id": "consolidate", "label": "Consolidate", "status": "running",
            "round": 3, "max_rounds": 10, "started_at": 1700.0,
            "activity": {"tool": "read_file", "target": "investigation.json", "at": 1712.0},
        }],
    })
    markup = store.render_markup()
    assert "Consolidate" in markup
    assert "3/10" in markup
    assert "investigation.json" in markup


def test_a_frame_without_the_new_fields_still_renders():
    """Older emitters and nested runs may omit them; the panel must not crash."""
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "running", "call_id": "workflow:r1",
        "arguments": {"workflow": "wf"},
        "nodes": [{"id": "a", "label": "A", "status": "running"}],
    })
    assert "A" in store.render_markup()


def test_running_node_round_without_max_rounds_is_not_shown():
    """`round` and `max_rounds` are a pair — the node's *visit* budget (`iteration`/
    `budget`) is a different axis and must never stand in as the denominator."""
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "running", "call_id": "workflow:r3",
        "arguments": {"workflow": "wf"},
        "nodes": [{
            "id": "n", "label": "N", "status": "running",
            "round": 3, "iteration": 2, "budget": 5,
        }],
    })
    markup = store.render_markup()
    assert "3/5" not in markup


def test_running_node_activity_target_escapes_markup():
    """A tool's target is arbitrary run text (a path, a shell command, a search
    query) and may contain literal brackets — they must not be parsed as Rich
    markup tags when rendered in the sidebar."""
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "running", "call_id": "workflow:r4",
        "arguments": {"workflow": "wf"},
        "nodes": [{
            "id": "n", "label": "N", "status": "running",
            "activity": {"tool": "grep", "target": "TODO[urgent]", "at": 1712.0},
        }],
    })
    markup = store.render_markup()
    assert r"TODO\[urgent]" in markup


def test_running_node_activity_tool_escapes_markup():
    """A tool name is text from the model's own tool-call response — the same
    trust class as `target` — and may contain literal brackets that must not
    be parsed as Rich markup tags when rendered in the sidebar."""
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "running", "call_id": "workflow:r9",
        "arguments": {"workflow": "wf"},
        "nodes": [{
            "id": "n", "label": "N", "status": "running",
            "activity": {"tool": "search[urgent]", "at": 1712.0},
        }],
    })
    markup = store.render_markup()
    assert r"search\[urgent]" in markup


def test_running_node_activity_tool_unmatched_closing_tag_does_not_raise():
    """An unmatched closing-tag pattern in a tool name must not crash the
    panel: Rich's markup parser raises MarkupError on a dangling closing tag
    when the string reaches it unescaped."""
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "running", "call_id": "workflow:r10",
        "arguments": {"workflow": "wf"},
        "nodes": [{
            "id": "n", "label": "N", "status": "running",
            "activity": {"tool": "search[/urgent]", "at": 1712.0},
        }],
    })
    markup = store.render_markup()
    rich_render(markup)  # must not raise rich.errors.MarkupError


def test_subagent_progress_tool_escapes_markup():
    """A sub-agent's in-flight tool name is the same model-produced text as a
    workflow node's activity tool — it may contain literal brackets that must
    not be parsed as Rich markup tags."""
    store = WorkStore()
    store.ingest({
        "name": "subagent_result", "phase": "running",
        "call_id": "subagent:t2", "label": "explore",
        "progress": {"iteration": 1, "tool": "search[urgent]"},
    })
    markup = store.render_markup()
    assert r"search\[urgent]" in markup


def test_subagent_progress_tool_unmatched_closing_tag_does_not_raise():
    """Same crash risk as the workflow-node activity path, for the sub-agent
    progress detail string."""
    store = WorkStore()
    store.ingest({
        "name": "subagent_result", "phase": "running",
        "call_id": "subagent:t3", "label": "explore",
        "progress": {"iteration": 1, "tool": "search[/urgent]"},
    })
    markup = store.render_markup()
    rich_render(markup)  # must not raise rich.errors.MarkupError


def test_running_node_elapsed_advances_against_an_injected_clock():
    """A running node's clock is a live diff of `started_at` against `now` — the
    test passes `now` explicitly (rather than sleeping) so the advance is
    deterministic: the same node, rendered at two different instants, shows two
    different elapsed values."""
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "running", "call_id": "workflow:r5",
        "arguments": {"workflow": "wf"},
        "nodes": [{"id": "n", "label": "N", "status": "running", "started_at": 1000.0}],
    })
    just_started = store.render_markup(now=1000.0)
    four_minutes_later = store.render_markup(now=1261.0)
    assert "0:00" in just_started
    assert "4:21" in four_minutes_later
    assert just_started != four_minutes_later


def test_finished_node_renders_duration_s():
    """A finished node's `duration_s` is already a span of seconds, not a
    timestamp — it is formatted as-is, with no dependency on the current clock."""
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "running", "call_id": "workflow:r6",
        "arguments": {"workflow": "wf"},
        "nodes": [
            {"id": "a", "label": "A", "status": "done", "duration_s": 125},
            {"id": "b", "label": "B", "status": "running"},
        ],
    })
    markup = store.render_markup()
    assert "2:05" in markup


def test_node_with_neither_time_field_renders_no_time_segment():
    """A node reporting neither `started_at` nor `duration_s` (not yet started,
    or from an older emitter) renders no time segment at all, and does not
    crash — exactly as before these fields existed."""
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "running", "call_id": "workflow:r7",
        "arguments": {"workflow": "wf"},
        "nodes": [
            {"id": "a", "label": "A", "status": "done"},
            {"id": "b", "label": "B", "status": "running"},
        ],
    })
    markup = store.render_markup()
    assert "A" in markup and "B" in markup
    assert not re.search(r"\d+:\d{2}", markup)  # no elapsed-shaped value (m:ss / h:mm:ss) anywhere


def test_elapsed_format_rolls_over_to_hours():
    """Past sixty minutes the format extends to h:mm:ss rather than overflowing
    the minutes field."""
    store = WorkStore()
    store.ingest({
        "name": "workflow_progress", "phase": "running", "call_id": "workflow:r8",
        "arguments": {"workflow": "wf"},
        "nodes": [
            {"id": "a", "label": "A", "status": "done", "duration_s": 3661},
            {"id": "b", "label": "B", "status": "running"},
        ],
    })
    markup = store.render_markup()
    assert "1:01:01" in markup
