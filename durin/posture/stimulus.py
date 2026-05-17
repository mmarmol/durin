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
            StimulusRule(StimulusEvent.STEP_FAILED, AxisName.CAUTELA, +0.10),
            StimulusRule(StimulusEvent.STEP_FAILED, AxisName.PROFUNDIDAD, +0.05),
            StimulusRule(StimulusEvent.STEP_SUCCEEDED, AxisName.CAUTELA, -0.03),
            StimulusRule(StimulusEvent.CONSECUTIVE_SUCCESSES_3, AxisName.EXPLORACION, +0.05),
            StimulusRule(StimulusEvent.CONSECUTIVE_FAILURES_3, AxisName.CAUTELA, +0.15),
            StimulusRule(StimulusEvent.CONSECUTIVE_FAILURES_3, AxisName.CONFORMIDAD, -0.10),
            StimulusRule(StimulusEvent.GOAL_AMBIGUOUS, AxisName.PROFUNDIDAD, +0.10),
            StimulusRule(StimulusEvent.USER_CORRECTED, AxisName.CONFORMIDAD, +0.05),
            StimulusRule(StimulusEvent.USER_APPROVED_RISKY, AxisName.CAUTELA, -0.05),
            StimulusRule(StimulusEvent.CRITICAL_ACTION, AxisName.CAUTELA, +0.10),
            StimulusRule(StimulusEvent.EXPLORATORY_TASK, AxisName.EXPLORACION, +0.10),
            StimulusRule(StimulusEvent.EXPLICIT_PROTOCOL, AxisName.DISCIPLINA, +0.10),
        ])
