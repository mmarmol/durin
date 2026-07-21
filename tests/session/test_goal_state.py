"""Tests for ``goal_state`` session metadata helpers."""

from __future__ import annotations

from durin.session.goal_state import (
    GOAL_STATE_KEY,
    discard_legacy_goal_state_key,
    goal_state_runtime_lines,
    goal_state_ws_blob,
    parse_goal_state,
    runner_wall_llm_timeout_s,
    sustained_goal_active,
)
from durin.session.manager import SessionManager


def test_runtime_lines_empty_when_no_metadata():
    assert goal_state_runtime_lines(None) == []
    assert goal_state_runtime_lines({}) == []


def test_runtime_lines_keep_a_compact_trace_when_completed():
    """A session rarely ends when its goal does. Rendering nothing dropped the
    objective from the anchor entirely — on one observed session the goal was
    completed three messages before the first compaction and 16 of 19
    compactions then ran with no objective in context at all."""
    meta = {
        GOAL_STATE_KEY: {
            "status": "completed",
            "objective": "was doing X",
            "recap": "shipped it",
        },
    }
    assert goal_state_runtime_lines(meta) == [
        "Goal (completed): was doing X",
        "Outcome: shipped it",
    ]


def test_completed_goal_prefers_the_ui_summary_and_stays_short():
    meta = {
        GOAL_STATE_KEY: {
            "status": "completed",
            "objective": "a very long objective\nspanning several lines",
            "ui_summary": "short label",
            "recap": "R" * 900,
        },
    }
    lines = goal_state_runtime_lines(meta)
    assert lines[0] == "Goal (completed): short label"
    assert lines[1].endswith("…")
    assert all(len(line) < 300 for line in lines)


def test_completed_goal_does_not_reactivate_the_wall_clock_backstop():
    """Rendering a finished goal must not make it look active — that flag gates
    the runner's wall-clock timeout, and cron/workflow sessions rely on it."""
    from durin.session.goal_state import sustained_goal_active

    meta = {GOAL_STATE_KEY: {"status": "completed", "objective": "done"}}
    assert goal_state_runtime_lines(meta)          # renders
    assert sustained_goal_active(meta) is False    # but is not active


def test_runtime_lines_empty_when_completed_goal_has_no_text():
    meta = {GOAL_STATE_KEY: {"status": "completed"}}
    assert goal_state_runtime_lines(meta) == []


def test_runtime_lines_include_objective_when_active():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "Ship the fix.",
            "ui_summary": "fix",
        },
    }
    lines = goal_state_runtime_lines(meta)
    assert "Goal (active):" in lines
    assert "Ship the fix." in lines
    assert any("Summary: fix" in ln for ln in lines)


def test_runtime_lines_read_legacy_thread_goal_key():
    meta = {"thread_goal": {"status": "active", "objective": "Legacy key.", "ui_summary": "L"}}
    lines = goal_state_runtime_lines(meta)
    assert "Legacy key." in lines


def test_goal_state_key_takes_precedence_over_legacy():
    meta = {
        GOAL_STATE_KEY: {"status": "active", "objective": "New key wins.", "ui_summary": "n"},
        "thread_goal": {"status": "active", "objective": "Ignored.", "ui_summary": "o"},
    }
    lines = goal_state_runtime_lines(meta)
    assert "New key wins." in lines
    assert "Ignored." not in "".join(lines)


def test_discard_legacy_goal_state_key():
    meta: dict = {"thread_goal": {"x": 1}, GOAL_STATE_KEY: {"status": "active"}}
    discard_legacy_goal_state_key(meta)
    assert "thread_goal" not in meta
    assert GOAL_STATE_KEY in meta


def test_parse_goal_state_accepts_json_string():
    assert parse_goal_state('{"status":"active","objective":"x"}') == {
        "status": "active",
        "objective": "x",
    }


def test_goal_state_ws_blob_inactive_when_missing_or_completed():
    assert goal_state_ws_blob(None) == {"active": False}
    assert goal_state_ws_blob({}) == {"active": False}
    assert goal_state_ws_blob({GOAL_STATE_KEY: {"status": "completed", "objective": "x"}}) == {
        "active": False,
    }


def test_goal_state_ws_blob_active_shape():
    meta = {
        GOAL_STATE_KEY: {
            "status": "active",
            "objective": "Build feature.",
            "ui_summary": "feat",
        },
    }
    assert goal_state_ws_blob(meta) == {
        "active": True,
        "ui_summary": "feat",
        "objective": "Build feature.",
    }


def test_sustained_goal_active_false_when_missing_or_completed():
    assert sustained_goal_active(None) is False
    assert sustained_goal_active({}) is False
    assert sustained_goal_active({GOAL_STATE_KEY: {"status": "completed", "objective": "x"}}) is False


def test_sustained_goal_active_true_when_active():
    meta = {GOAL_STATE_KEY: {"status": "active", "objective": "Run long task."}}
    assert sustained_goal_active(meta) is True


def test_sustained_goal_active_respects_legacy_thread_goal_key():
    meta = {"thread_goal": {"status": "active", "objective": "Legacy."}}
    assert sustained_goal_active(meta) is True


def test_runner_wall_llm_timeout_uses_metadata_override(tmp_path):
    sm = SessionManager(tmp_path)
    assert (
        runner_wall_llm_timeout_s(
            sm,
            "cli:test",
            metadata={GOAL_STATE_KEY: {"status": "active", "objective": "x"}},
        )
        == 0.0
    )
    assert runner_wall_llm_timeout_s(sm, "cli:test", metadata={}) is None


def test_runner_wall_llm_timeout_reads_session_when_metadata_missing(tmp_path):
    sm = SessionManager(tmp_path)
    sess = sm.get_or_create("c:d")
    sess.metadata = {GOAL_STATE_KEY: {"status": "active", "objective": "z"}}
    assert runner_wall_llm_timeout_s(sm, "c:d") == 0.0
    sess.metadata = {}
    assert runner_wall_llm_timeout_s(sm, "c:d") is None


def test_ws_blob_includes_mode_and_pending_question():
    """Non-default agent mode and a pending ask_user question ride the
    goal_state frame so the webui can render badges/strips."""
    meta = {
        "agent_mode": "plan",
        "pending_question": {
            "question_id": "q", "question": "Color?", "options": ["red"],
        },
    }
    blob = goal_state_ws_blob(meta)
    assert blob["mode"] == "plan"
    assert blob["pending_question"] == {"question": "Color?", "options": ["red"]}


def test_ws_blob_defaults_omit_mode_and_question():
    blob = goal_state_ws_blob({})
    assert "pending_question" not in blob
    assert "mode" not in blob
    # Default mode is omitted to keep the frame small.
    assert "mode" not in goal_state_ws_blob({"agent_mode": "build"})
