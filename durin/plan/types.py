"""Core types for the plan system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


class ExecutionTier(StrEnum):
    DIRECT = "direct"
    EXECUTE_VERIFY = "execute_verify"
    FULL_PLAN = "full_plan"


class Phase(StrEnum):
    INVESTIGATE = "investigate"
    PLAN = "plan"
    EXECUTE = "execute"
    CONFIRM = "confirm"


PHASE_ORDER: tuple[Phase, ...] = (
    Phase.INVESTIGATE,
    Phase.PLAN,
    Phase.EXECUTE,
    Phase.CONFIRM,
)


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
