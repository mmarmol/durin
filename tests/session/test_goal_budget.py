"""Turn budget on sustained goals (long_task max_turns) — surfacing only."""

from __future__ import annotations

from durin.session.goal_state import (
    GOAL_STATE_KEY,
    goal_state_runtime_lines,
    increment_goal_turns,
)


def _active_goal(max_turns=None, turns_used=0):
    goal = {"status": "active", "objective": "ship the feature"}
    if max_turns is not None:
        goal["max_turns"] = max_turns
        goal["turns_used"] = turns_used
    return goal


class TestIncrementGoalTurns:
    def test_increments_active_budgeted_goal(self):
        meta = {GOAL_STATE_KEY: _active_goal(max_turns=5, turns_used=2)}
        increment_goal_turns(meta)
        assert meta[GOAL_STATE_KEY]["turns_used"] == 3

    def test_noop_without_budget(self):
        meta = {GOAL_STATE_KEY: _active_goal()}
        increment_goal_turns(meta)
        assert "turns_used" not in meta[GOAL_STATE_KEY]

    def test_noop_when_inactive_or_missing(self):
        meta = {GOAL_STATE_KEY: {"status": "completed", "max_turns": 5, "turns_used": 1}}
        increment_goal_turns(meta)
        assert meta[GOAL_STATE_KEY]["turns_used"] == 1
        increment_goal_turns({})  # must not raise
        increment_goal_turns(None)  # must not raise


class TestBudgetRuntimeLines:
    def test_budget_line_shown(self):
        meta = {GOAL_STATE_KEY: _active_goal(max_turns=10, turns_used=3)}
        joined = "\n".join(goal_state_runtime_lines(meta))
        assert "Turn budget: 3/10" in joined
        assert "exceeded" not in joined.lower()

    def test_exceeded_line_instructs_wrap_up(self):
        meta = {GOAL_STATE_KEY: _active_goal(max_turns=4, turns_used=4)}
        joined = "\n".join(goal_state_runtime_lines(meta))
        assert "Turn budget: 4/4" in joined
        assert "complete_goal" in joined

    def test_no_budget_no_line(self):
        meta = {GOAL_STATE_KEY: _active_goal()}
        assert "Turn budget" not in "\n".join(goal_state_runtime_lines(meta))
