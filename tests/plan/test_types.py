"""Tests for plan system types."""

from durin.plan.types import (
    ExecutionTier, Phase, PlanItem, PlanState,
    PHASE_ORDER, PHASE_TEMPERATURE,
)


class TestExecutionTier:
    def test_values(self):
        assert ExecutionTier.DIRECT == "direct"
        assert ExecutionTier.PLAN == "plan"


class TestPhase:
    def test_order(self):
        assert PHASE_ORDER == (Phase.INVESTIGATE, Phase.PLAN, Phase.EXECUTE, Phase.VERIFY)

    def test_temperature_mapping(self):
        assert PHASE_TEMPERATURE[Phase.INVESTIGATE] == 0.5
        assert PHASE_TEMPERATURE[Phase.PLAN] == 0.4
        assert PHASE_TEMPERATURE[Phase.EXECUTE] == 0.15
        assert PHASE_TEMPERATURE[Phase.VERIFY] == 0.1


class TestPlanState:
    def test_default(self):
        state = PlanState(goal="fix bug")
        assert state.tier == ExecutionTier.DIRECT
        assert state.items == []
        assert state.current_phase is None
        assert state.cycle_count == 0
        assert state.edit_detected is False
        assert state.verify_passed is False

    def test_has_pending_items(self):
        state = PlanState(goal="x", items=[PlanItem("step1"), PlanItem("step2")])
        assert state.has_pending_items is True

    def test_all_done(self):
        state = PlanState(goal="x", items=[
            PlanItem("step1", status="done"),
            PlanItem("step2", status="done"),
        ])
        assert state.all_done is True

    def test_all_done_empty(self):
        state = PlanState(goal="x")
        assert state.all_done is False

    def test_next_phase_from_none(self):
        state = PlanState(goal="x")
        assert state.next_phase() == Phase.INVESTIGATE

    def test_next_phase_cycles(self):
        state = PlanState(goal="x", current_phase=Phase.INVESTIGATE)
        assert state.next_phase() == Phase.PLAN
        state.current_phase = Phase.PLAN
        assert state.next_phase() == Phase.EXECUTE
        state.current_phase = Phase.EXECUTE
        assert state.next_phase() == Phase.VERIFY
        state.current_phase = Phase.VERIFY
        assert state.next_phase() == Phase.INVESTIGATE
