"""Core types for the plan system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


class ExecutionTier(StrEnum):
    DIRECT = "direct"
    PLAN = "plan"


class Phase(StrEnum):
    INVESTIGATE = "investigate"
    PLAN = "plan"
    EXECUTE = "execute"
    VERIFY = "verify"


PHASE_ORDER: tuple[Phase, ...] = (
    Phase.INVESTIGATE,
    Phase.PLAN,
    Phase.EXECUTE,
    Phase.VERIFY,
)


PHASE_TEMPERATURE: dict[Phase, float] = {
    Phase.INVESTIGATE: 0.5,
    Phase.PLAN: 0.4,
    Phase.EXECUTE: 0.15,
    Phase.VERIFY: 0.1,
}


@dataclass
class PlanItem:
    description: str
    status: Literal["pending", "in_progress", "done", "failed"] = "pending"
    added_at_cycle: int = 1
    completed_at_cycle: int | None = None


@dataclass
class PlanState:
    goal: str
    tier: ExecutionTier = ExecutionTier.DIRECT
    items: list[PlanItem] = field(default_factory=list)
    current_phase: Phase | None = None
    cycle_count: int = 0
    last_failure_context: str = ""
    edit_detected: bool = False
    verify_passed: bool = False

    @property
    def has_pending_items(self) -> bool:
        return any(i.status in ("pending", "in_progress") for i in self.items)

    @property
    def all_done(self) -> bool:
        return bool(self.items) and all(i.status == "done" for i in self.items)

    def next_phase(self) -> Phase:
        if self.current_phase is None:
            return Phase.INVESTIGATE
        idx = PHASE_ORDER.index(self.current_phase)
        return PHASE_ORDER[(idx + 1) % len(PHASE_ORDER)]
