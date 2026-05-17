"""PostureHook — lifecycle hook that bridges the posture vector into the agent loop."""

from __future__ import annotations

from typing import TYPE_CHECKING

from durin.agent.hook import AgentHook, AgentHookContext
from durin.deliberation.constants import CRITICAL_TOOLS
from durin.posture.goal_bias import compute_goal_bias
from durin.posture.homeostasis import update_vector
from durin.posture.phrase import generate_posture_phrase
from durin.posture.stimulus import StimulusEvent, StimulusTable
from durin.posture.vector import AxisName, PostureVector

if TYPE_CHECKING:
    from durin.telemetry.logger import TelemetryLogger


_PROTOCOL_MARKERS: frozenset[str] = frozenset({
    "## steps", "## checklist", "## protocol", "## procedure",
    "## procedimiento", "## pasos", "paso 1", "step 1",
    "## compliance", "## normativa",
})


class PostureHook(AgentHook):
    """Tracks posture vector state and detects stimulus events from iteration outcomes."""

    __slots__ = ("_vector", "_table", "_consecutive_failures", "_consecutive_successes", "_telemetry", "_protocol_detected")

    def __init__(
        self,
        vector: PostureVector,
        table: StimulusTable | None = None,
        telemetry: TelemetryLogger | None = None,
    ) -> None:
        super().__init__()
        self._vector = vector
        self._table = table or StimulusTable.default()
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._telemetry = telemetry
        self._protocol_detected = False

    @property
    def current_vector(self) -> PostureVector:
        return self._vector

    @property
    def current_phrase(self) -> str:
        return generate_posture_phrase(self._vector)

    async def before_iteration(self, context: AgentHookContext) -> None:
        if context.iteration == 0:
            goal_deltas = self._apply_goal_bias(context)
            protocol_deltas = self._apply_protocol_bias(context)
            all_deltas = {**goal_deltas, **{
                k: goal_deltas.get(k, 0) + v
                for k, v in protocol_deltas.items()
            }} if protocol_deltas else goal_deltas
            snapshot = self._vector.snapshot()
            if context.emit_ui:
                await context.emit_ui("posture_update", {
                    "axes": {k: v for k, v in snapshot.items()},
                    "deltas": all_deltas,
                })
            if self._telemetry:
                self._telemetry.log_posture_initial(
                    {k: round(v, 4) for k, v in snapshot.items()}
                )

    async def after_iteration(self, context: AgentHookContext) -> None:
        events = self._detect_events(context)
        if not events:
            return
        prev_snapshot = self._vector.snapshot()
        deltas = self._table.resolve(events)
        self._vector = update_vector(self._vector, deltas)
        new_snapshot = self._vector.snapshot()
        if prev_snapshot != new_snapshot:
            computed_deltas = {
                k: round(new_snapshot[k] - prev_snapshot[k], 4)
                for k in new_snapshot
                if abs(new_snapshot[k] - prev_snapshot[k]) > 0.001
            }
            if context.emit_ui:
                await context.emit_ui("posture_update", {
                    "axes": {k: v for k, v in new_snapshot.items()},
                    "deltas": computed_deltas,
                })
            if self._telemetry:
                self._telemetry.log_posture_change(
                    axes={k: round(v, 4) for k, v in new_snapshot.items()},
                    deltas=computed_deltas,
                    events=[e.value for e in events],
                )

    def _detect_events(self, context: AgentHookContext) -> set[StimulusEvent]:
        events: set[StimulusEvent] = set()

        has_error = context.error is not None
        has_tool_failure = any(
            self._is_tool_failure(result) for result in context.tool_results
        )

        if has_error or has_tool_failure:
            events.add(StimulusEvent.STEP_FAILED)
            self._consecutive_failures += 1
            self._consecutive_successes = 0
            if self._consecutive_failures >= 3:
                events.add(StimulusEvent.CONSECUTIVE_FAILURES_3)
        elif context.tool_calls:
            events.add(StimulusEvent.STEP_SUCCEEDED)
            self._consecutive_successes += 1
            self._consecutive_failures = 0
            if self._consecutive_successes >= 3:
                events.add(StimulusEvent.CONSECUTIVE_SUCCESSES_3)

        if context.injected_messages_count > 0:
            events.add(StimulusEvent.USER_CORRECTED)

        if (context.iteration > 0
                and not context.tool_calls
                and not context.final_content
                and context.error is None):
            events.add(StimulusEvent.GOAL_AMBIGUOUS)

        if any(tc.name in CRITICAL_TOOLS for tc in context.tool_calls):
            events.add(StimulusEvent.CRITICAL_ACTION)

        if not self._protocol_detected and self._has_protocol_markers(context):
            events.add(StimulusEvent.EXPLICIT_PROTOCOL)
            self._protocol_detected = True

        return events

    @staticmethod
    def _has_protocol_markers(context: AgentHookContext) -> bool:
        """Check if system prompt contains structured protocol markers."""
        for msg in context.messages:
            if msg.get("role") != "system":
                continue
            content = msg.get("content", "").lower()
            if any(marker in content for marker in _PROTOCOL_MARKERS):
                return True
        return False

    def _apply_goal_bias(self, context: AgentHookContext) -> dict[str, float]:
        """Extract goal from first user message and apply cold-start bias."""
        goal_text = self._extract_goal_text(context)
        if not goal_text:
            return {}
        deltas = compute_goal_bias(goal_text)
        if deltas:
            self._vector = update_vector(self._vector, deltas)
        return {
            k.value if hasattr(k, "value") else k: round(v, 4)
            for k, v in deltas.items()
        } if deltas else {}

    def _apply_protocol_bias(self, context: AgentHookContext) -> dict[str, float]:
        """Detect protocol markers at session start and apply disciplina bias."""
        if self._protocol_detected:
            return {}
        if not self._has_protocol_markers(context):
            return {}
        self._protocol_detected = True
        deltas = {AxisName.DISCIPLINA: 0.10}
        self._vector = update_vector(self._vector, deltas)
        return {"disciplina": 0.10}

    @staticmethod
    def _extract_goal_text(context: AgentHookContext) -> str:
        for msg in reversed(context.messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content[:500]
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            return block.get("text", "")[:500]
        return ""

    @staticmethod
    def _is_tool_failure(result: object) -> bool:
        if isinstance(result, dict):
            if result.get("error"):
                return True
            if result.get("is_error"):
                return True
        return False
