"""Tests for the task-state anchor assembly."""
from __future__ import annotations

from durin.agent.task_state import task_state_runtime_lines
from durin.session.decision_log import add_decision


def test_empty_metadata_yields_no_block():
    assert task_state_runtime_lines({}) == []
    assert task_state_runtime_lines(None) == []


def test_decisions_only_renders_wrapped_section():
    meta: dict = {}
    add_decision(meta, "chose X over Y", source="tool", ts="t1")
    lines = task_state_runtime_lines(meta)
    assert lines[0] == "<task-state>"
    assert lines[-1] == "</task-state>"
    assert "## Decisions & findings" in lines
    assert "  - chose X over Y" in lines
    assert "## Goal" not in lines          # no goal set
    assert "## Current focus" not in lines  # no todos/plan set


def test_todos_render_under_current_focus():
    meta = {"todos": [{"content": "do thing", "status": "pending", "activeForm": "doing thing"}]}
    lines = task_state_runtime_lines(meta)
    assert "## Current focus" in lines
    assert "do thing" in "\n".join(lines)
