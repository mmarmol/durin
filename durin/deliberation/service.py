"""DeliberationService — invoked by PlanHook at INVESTIGATE→PLAN transition."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from loguru import logger

from durin.deliberation.engine import DeliberationEngine
from durin.deliberation.synthesis import render_for_injection
from durin.deliberation.types import DeliberationContext, DeliberationResult, HistoryEntry

if TYPE_CHECKING:
    from durin.telemetry.logger import TelemetryLogger


class DeliberationService:
    """Stateless service that runs deliberation and logs results."""

    __slots__ = ("_engine", "_telemetry", "_history")

    def __init__(
        self,
        engine: DeliberationEngine,
        telemetry: "TelemetryLogger | None" = None,
    ) -> None:
        self._engine = engine
        self._telemetry = telemetry
        self._history: list[HistoryEntry] = []

    @property
    def history(self) -> list[HistoryEntry]:
        return list(self._history)

    async def deliberate(
        self,
        context: DeliberationContext,
        trigger: str = "investigate_to_plan",
        cycle: int = 1,
    ) -> DeliberationResult:
        """Run deliberation engine and log full output to telemetry."""
        logger.info("Deliberation [{}]: starting (cycle={})", trigger, cycle)

        result = await self._engine.deliberate(context)

        logger.info(
            "Deliberation [{}]: done in {:.0f}ms, {} perspectives, model={}",
            trigger,
            result.duration_ms,
            len(result.perspectives),
            result.model,
        )

        self._history.append(HistoryEntry(
            timestamp=time.time(),
            trigger=trigger,
            synthesis_brief=result.synthesis[:200],
            perspectives_count=len(result.perspectives),
            duration_ms=result.duration_ms,
            cycle=cycle,
        ))

        if self._telemetry:
            self._telemetry.log_deliberation_v3(
                trigger=trigger,
                cycle=cycle,
                model=result.model,
                duration_ms=result.duration_ms,
                posture=context.posture_snapshot,
                perspectives={p.role: p.content for p in result.perspectives},
                synthesis=result.synthesis,
            )

        return result

    def render(self, result: DeliberationResult) -> str:
        """Render result for injection into agent context."""
        return render_for_injection(result)
