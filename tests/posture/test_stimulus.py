"""Tests for stimulus table."""

from __future__ import annotations

from durin.posture.stimulus import StimulusEvent, StimulusRule, StimulusTable
from durin.posture.vector import AxisName


class TestStimulusTable:
    def test_default_has_twelve_rules(self):
        table = StimulusTable.default()
        assert len(table.rules) == 28

    def test_default_covers_all_events(self):
        table = StimulusTable.default()
        events_covered = {rule.event for rule in table.rules}
        assert StimulusEvent.STEP_FAILED in events_covered
        # STEP_SUCCEEDED deliberately has no rule (too weak a signal for posture change)
        assert StimulusEvent.CONSECUTIVE_SUCCESSES_3 in events_covered
        assert StimulusEvent.CONSECUTIVE_FAILURES_3 in events_covered
        assert StimulusEvent.GOAL_AMBIGUOUS in events_covered
        assert StimulusEvent.USER_CORRECTED in events_covered
        assert StimulusEvent.USER_APPROVED_RISKY in events_covered
        assert StimulusEvent.CRITICAL_ACTION in events_covered
        assert StimulusEvent.EXPLORATORY_TASK in events_covered
        assert StimulusEvent.EXPLICIT_PROTOCOL in events_covered

    def test_resolve_step_succeeded_has_no_effect(self):
        table = StimulusTable.default()
        deltas = table.resolve({StimulusEvent.STEP_SUCCEEDED})
        assert deltas == {}

    def test_resolve_event_with_multiple_axis_effects(self):
        table = StimulusTable.default()
        deltas = table.resolve({StimulusEvent.STEP_FAILED})
        assert AxisName.CAUTION in deltas
        assert AxisName.DEPTH in deltas
        assert deltas[AxisName.CAUTION] == 0.10
        assert deltas[AxisName.DEPTH] == 0.05

    def test_resolve_multiple_events_sums_same_axis(self):
        table = StimulusTable.default()
        deltas = table.resolve({
            StimulusEvent.STEP_FAILED,
            StimulusEvent.CRITICAL_ACTION,
        })
        assert deltas[AxisName.CAUTION] == 0.10 + 0.10

    def test_resolve_empty_events_returns_empty(self):
        table = StimulusTable.default()
        deltas = table.resolve(set())
        assert deltas == {}

    def test_resolve_unknown_event_not_in_table(self):
        table = StimulusTable([
            StimulusRule(StimulusEvent.STEP_FAILED, AxisName.CAUTION, +0.10),
        ])
        deltas = table.resolve({StimulusEvent.STEP_SUCCEEDED})
        assert deltas == {}

    def test_with_rules_extends_table(self):
        table = StimulusTable.default()
        extra = [StimulusRule(StimulusEvent.STEP_FAILED, AxisName.DISCIPLINE, +0.05)]
        extended = table.with_rules(extra)

        assert len(extended.rules) == 29
        deltas = extended.resolve({StimulusEvent.STEP_FAILED})
        assert AxisName.DISCIPLINE in deltas

    def test_with_rules_does_not_modify_original(self):
        table = StimulusTable.default()
        table.with_rules([StimulusRule(StimulusEvent.STEP_FAILED, AxisName.DISCIPLINE, +0.05)])
        assert len(table.rules) == 28

    def test_consecutive_failures_affects_cautela_and_conformidad(self):
        table = StimulusTable.default()
        deltas = table.resolve({StimulusEvent.CONSECUTIVE_FAILURES_3})
        assert deltas[AxisName.CAUTION] == 0.15
        assert deltas[AxisName.CONFORMITY] == -0.10

    def test_delta_signs_match_spec(self):
        table = StimulusTable.default()
        for rule in table.rules:
            if rule.event == StimulusEvent.STEP_FAILED:
                assert rule.delta > 0
            if rule.event == StimulusEvent.USER_APPROVED_RISKY:
                assert rule.delta < 0
