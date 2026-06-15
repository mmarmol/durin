"""Assemble the task-state anchor injected into Runtime Context every turn.

Groups the existing, compaction-surviving runtime lines (goal, todos, executing
plan) under one ``<task-state>`` frame and adds the decision log. Pure
presentation: storage for goal/todos/plan is unchanged. Empty sections are
omitted; an all-empty anchor yields no lines at all.

See docs/architecture/loop.md and
.workdocs/superpowers/specs/2026-06-15-task-state-anchor-design.md.
"""
from __future__ import annotations

from typing import Any, Mapping

from durin.session.decision_log import decision_log_runtime_lines
from durin.session.goal_state import goal_state_runtime_lines
from durin.session.todo_state import todos_runtime_lines


def task_state_runtime_lines(metadata: Mapping[str, Any] | None) -> list[str]:
    """Return the ``<task-state>`` block lines, or [] when every section is empty."""
    # Imported lazily to mirror build_messages and avoid an import cycle with
    # durin.agent.agent_mode.
    from durin.agent.agent_mode import (
        executing_plan_runtime_lines,
        plan_mode_runtime_lines,
    )

    goal = list(goal_state_runtime_lines(metadata))
    decisions = list(decision_log_runtime_lines(metadata))
    focus = (
        list(todos_runtime_lines(metadata))
        + list(plan_mode_runtime_lines(metadata))
        + list(executing_plan_runtime_lines(metadata))
    )
    if not (goal or decisions or focus):
        return []

    lines: list[str] = ["<task-state>"]
    if goal:
        lines.append("## Goal")
        lines.extend(goal)
    if decisions:
        lines.append("## Decisions & findings")
        lines.extend(decisions)
    if focus:
        lines.append("## Current focus")
        lines.extend(focus)
    lines.append("</task-state>")
    return lines
