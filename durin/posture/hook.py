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
    "step 1", "## compliance",
})


class PostureHook(AgentHook):
    """Tracks posture vector state and detects stimulus events from iteration outcomes."""

    __slots__ = (
        "_vector", "_table", "_consecutive_failures", "_consecutive_successes",
        "_telemetry", "_protocol_detected", "_edited_files", "_iters_since_edit",
        "_recent_tool_types",
    )

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
        self._edited_files: set[str] = set()
        self._iters_since_edit: int = 0
        self._recent_tool_types: list[str] = []

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

        # --- New stimuli ---
        tool_names = [tc.name for tc in context.tool_calls]

        # MULTI_FILE_EDIT: track distinct files edited
        for tc in context.tool_calls:
            if tc.name == "edit_file":
                args = tc.arguments if isinstance(tc.arguments, dict) else {}
                path = args.get("file_path") or args.get("path", "")
                if path:
                    self._edited_files.add(path)
        if len(self._edited_files) > 2:
            events.add(StimulusEvent.MULTI_FILE_EDIT)

        # STUCK_NO_PROGRESS: many iterations without an edit
        if "edit_file" in tool_names or "write_file" in tool_names:
            self._iters_since_edit = 0
        else:
            self._iters_since_edit += 1
        if self._iters_since_edit >= 10:
            events.add(StimulusEvent.STUCK_NO_PROGRESS)

        # VALIDATION_SUCCESS / VALIDATION_FAILURE: exec results
        for i, tc in enumerate(context.tool_calls):
            if tc.name != "exec":
                continue
            result = context.tool_results[i] if i < len(context.tool_results) else None
            if self._is_validation_exec(tc, result):
                if self._is_tool_failure(result):
                    events.add(StimulusEvent.VALIDATION_FAILURE)
                else:
                    events.add(StimulusEvent.VALIDATION_SUCCESS)

        # PHASE_TRANSITION: detect shift from exploration to implementation
        phase = self._classify_tool_phase(tool_names)
        if phase:
            self._recent_tool_types.append(phase)
            if len(self._recent_tool_types) > 10:
                self._recent_tool_types = self._recent_tool_types[-10:]
            if len(self._recent_tool_types) >= 4:
                prev = self._recent_tool_types[-4:-2]
                curr = self._recent_tool_types[-2:]
                if all(p == "explore" for p in prev) and all(p == "implement" for p in curr):
                    events.add(StimulusEvent.PHASE_TRANSITION)

        # External events injected by other hooks (e.g. PlanHook)
        for event_name in context.external_stimulus_events:
            try:
                events.add(StimulusEvent(event_name))
            except ValueError:
                pass

        return events

    @staticmethod
    def _classify_tool_phase(tool_names: list[str]) -> str | None:
        explore = {"read_file", "grep", "list_dir", "web_search", "web_fetch"}
        implement = {"edit_file", "write_file"}
        if any(t in implement for t in tool_names):
            return "implement"
        if any(t in explore for t in tool_names):
            return "explore"
        return None

    @staticmethod
    def _is_validation_exec(tc: object, result: object) -> bool:
        args = tc.arguments if isinstance(tc.arguments, dict) else {}
        cmd = args.get("command", "")
        validation_markers = ("test", "pytest", "unittest", "check", "verify", "assert")
        return any(m in cmd.lower() for m in validation_markers)

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
        """Detect protocol markers at session start and apply discipline bias."""
        if self._protocol_detected:
            return {}
        if not self._has_protocol_markers(context):
            return {}
        self._protocol_detected = True
        deltas = {AxisName.DISCIPLINE: 0.10}
        self._vector = update_vector(self._vector, deltas)
        return {"discipline": 0.10}

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
