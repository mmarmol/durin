"""Deliberation V3 data types — all immutable."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Perspective:
    """A single perspective from the multi-perspective analysis."""

    role: str  # "critico" | "explorador" | "pragmatico"
    content: str


@dataclass(frozen=True, slots=True)
class DeliberationResult:
    """Complete result of a single-call deliberation."""

    perspectives: tuple[Perspective, ...]
    synthesis: str
    duration_ms: float = 0.0
    model: str = ""


@dataclass(frozen=True, slots=True)
class DeliberationContext:
    """Context passed to the deliberation engine."""

    goal_summary: str
    investigation_context: str
    posture_snapshot: dict[str, float] = field(default_factory=dict)
    previous_failure: str = ""


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """Record of a past deliberation for observability."""

    timestamp: float
    trigger: str
    synthesis_brief: str
    perspectives_count: int = 3
    duration_ms: float = 0.0
    cycle: int = 1
