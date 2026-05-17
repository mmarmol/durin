"""Stimulus table — stateless event-to-delta mapping."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from durin.posture.vector import AxisName


class StimulusEvent(StrEnum):
    STEP_FAILED = "step_failed"
    STEP_SUCCEEDED = "step_succeeded"
    CONSECUTIVE_SUCCESSES_3 = "consecutive_successes_3"
    CONSECUTIVE_FAILURES_3 = "consecutive_failures_3"
    GOAL_AMBIGUOUS = "goal_ambiguous"
    USER_CORRECTED = "user_corrected"
    USER_APPROVED_RISKY = "user_approved_risky"
    CRITICAL_ACTION = "critical_action"
    EXPLORATORY_TASK = "exploratory_task"
    EXPLICIT_PROTOCOL = "explicit_protocol"
    MULTI_FILE_EDIT = "multi_file_edit"
    VALIDATION_SUCCESS = "validation_success"
    VALIDATION_FAILURE = "validation_failure"
    STUCK_NO_PROGRESS = "stuck_no_progress"
    PHASE_TRANSITION = "phase_transition"
    VERIFY_PASS = "verify_pass"
    VERIFY_FAIL = "verify_fail"
    CYCLE_RESTART = "cycle_restart"
    PLAN_COMPLEX = "plan_complex"


@dataclass(frozen=True)
class StimulusRule:
    event: StimulusEvent
    axis: AxisName
    delta: float


class StimulusTable:
    __slots__ = ("_rules",)

    def __init__(self, rules: list[StimulusRule]) -> None:
        self._rules = list(rules)

    @property
    def rules(self) -> list[StimulusRule]:
        return list(self._rules)

    def resolve(self, events: set[StimulusEvent]) -> dict[AxisName, float]:
        deltas: dict[AxisName, float] = {}
        for rule in self._rules:
            if rule.event in events:
                deltas[rule.axis] = deltas.get(rule.axis, 0.0) + rule.delta
        return deltas

    def with_rules(self, extra: list[StimulusRule]) -> StimulusTable:
        return StimulusTable(self._rules + extra)

    @classmethod
    def default(cls) -> StimulusTable:
        return cls([
            StimulusRule(StimulusEvent.STEP_FAILED, AxisName.CAUTION, +0.10),
            StimulusRule(StimulusEvent.STEP_FAILED, AxisName.DEPTH, +0.05),
            StimulusRule(StimulusEvent.CONSECUTIVE_SUCCESSES_3, AxisName.EXPLORATION, +0.02),
            StimulusRule(StimulusEvent.CONSECUTIVE_SUCCESSES_3, AxisName.DEPTH, -0.03),
            StimulusRule(StimulusEvent.CONSECUTIVE_FAILURES_3, AxisName.CAUTION, +0.15),
            StimulusRule(StimulusEvent.CONSECUTIVE_FAILURES_3, AxisName.CONFORMITY, -0.10),
            StimulusRule(StimulusEvent.GOAL_AMBIGUOUS, AxisName.DEPTH, +0.10),
            StimulusRule(StimulusEvent.USER_CORRECTED, AxisName.CONFORMITY, +0.05),
            StimulusRule(StimulusEvent.USER_APPROVED_RISKY, AxisName.CAUTION, -0.05),
            StimulusRule(StimulusEvent.CRITICAL_ACTION, AxisName.CAUTION, +0.10),
            StimulusRule(StimulusEvent.EXPLORATORY_TASK, AxisName.EXPLORATION, +0.10),
            StimulusRule(StimulusEvent.EXPLICIT_PROTOCOL, AxisName.DISCIPLINE, +0.10),
            StimulusRule(StimulusEvent.MULTI_FILE_EDIT, AxisName.DISCIPLINE, +0.08),
            StimulusRule(StimulusEvent.VALIDATION_SUCCESS, AxisName.CAUTION, -0.05),
            StimulusRule(StimulusEvent.VALIDATION_SUCCESS, AxisName.EXPLORATION, -0.03),
            StimulusRule(StimulusEvent.VALIDATION_FAILURE, AxisName.CAUTION, +0.10),
            StimulusRule(StimulusEvent.VALIDATION_FAILURE, AxisName.DEPTH, +0.08),
            StimulusRule(StimulusEvent.STUCK_NO_PROGRESS, AxisName.EXPLORATION, +0.10),
            StimulusRule(StimulusEvent.STUCK_NO_PROGRESS, AxisName.DEPTH, +0.10),
            StimulusRule(StimulusEvent.PHASE_TRANSITION, AxisName.DEPTH, -0.10),
            StimulusRule(StimulusEvent.VERIFY_PASS, AxisName.CAUTION, -0.10),
            StimulusRule(StimulusEvent.VERIFY_PASS, AxisName.EXPLORATION, -0.05),
            StimulusRule(StimulusEvent.VERIFY_FAIL, AxisName.CAUTION, +0.15),
            StimulusRule(StimulusEvent.VERIFY_FAIL, AxisName.DEPTH, +0.10),
            StimulusRule(StimulusEvent.CYCLE_RESTART, AxisName.DISCIPLINE, +0.05),
            StimulusRule(StimulusEvent.CYCLE_RESTART, AxisName.EXPLORATION, +0.10),
            StimulusRule(StimulusEvent.PLAN_COMPLEX, AxisName.DEPTH, +0.10),
            StimulusRule(StimulusEvent.PLAN_COMPLEX, AxisName.CAUTION, +0.05),
        ])
