"""DeliberationHook — lifecycle hook that triggers multi-generator deliberation."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from loguru import logger

from durin.agent.hook import AgentHook, AgentHookContext
from durin.deliberation.constants import CRITICAL_TOOLS
from durin.deliberation.engine import DeliberationEngine
from durin.deliberation.history import VerdictHistory
from durin.deliberation.synthesis import render_synthesis, synthesize
from durin.deliberation.types import (
    DeliberationContext, SynthesisResult, TriggerReason, Verdict, VerdictEntry,
)

if TYPE_CHECKING:
    from durin.telemetry.logger import TelemetryLogger



_DELIBERATION_TAG = "[Deliberación pre-análisis]"


class DeliberationHook(AgentHook):
    """Triggers deliberation at planning moments and critical actions.

    - PLANNING_MOMENT: fires in before_iteration(0), injects synthesis into
      the system prompt BEFORE the first LLM call.
    - CRITICAL_ACTION: fires in before_execute_tools when a dangerous tool
      is about to run, updates synthesis for subsequent iterations.
    """

    __slots__ = (
        "_engine", "_last_verdict", "_last_synthesis", "_last_synthesis_result",
        "_posture_snapshot_fn", "_telemetry",
        "_posture_at_last_deliberation", "_drift_threshold", "_history",
        "_last_delib_iteration",
    )

    def __init__(
        self,
        engine: DeliberationEngine,
        posture_snapshot_fn: object | None = None,
        telemetry: TelemetryLogger | None = None,
        drift_threshold: float = 0.15,
        history: VerdictHistory | None = None,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._last_verdict: Verdict | None = None
        self._last_synthesis: str | None = None
        self._last_synthesis_result: SynthesisResult | None = None
        self._posture_snapshot_fn = posture_snapshot_fn
        self._telemetry = telemetry
        self._posture_at_last_deliberation: dict[str, float] | None = None
        self._drift_threshold = drift_threshold
        self._history = history or VerdictHistory()
        self._last_delib_iteration: int = -10

    @property
    def last_verdict(self) -> Verdict | None:
        return self._last_verdict

    @property
    def last_synthesis(self) -> str | None:
        return self._last_synthesis

    @property
    def last_synthesis_result(self) -> SynthesisResult | None:
        return self._last_synthesis_result

    @property
    def history(self) -> VerdictHistory:
        return self._history

    async def before_iteration(self, context: AgentHookContext) -> None:
        # V2: Only deliberate once at the start (iter 0).
        # No re-deliberation on drift — the perspectives are seed context,
        # not live guidance that needs updating mid-execution.
        if context.iteration != 0:
            return

        if self._has_active_goal(context):
            if self._telemetry:
                self._telemetry.log_deliberation_skipped("goal_active")
            return

        await self._run_deliberation(context, TriggerReason.PLANNING_MOMENT)

        if self._last_synthesis:
            self._inject_as_pre_message(context)

    async def before_execute_tools(self, context: AgentHookContext) -> None:
        # V2: No longer triggers on critical_action.
        # Deliberation fires only at planning_moment (iter 0) and on posture drift.
        # The main model has full context to judge tool safety itself.
        pass

    def _should_redeliberate(self) -> bool:
        """Check if posture drifted enough since last deliberation to warrant re-evaluation."""
        if self._posture_at_last_deliberation is None:
            return False
        current = self._get_posture_snapshot()
        if not current:
            return False
        max_drift = max(
            abs(current.get(k, 0) - v)
            for k, v in self._posture_at_last_deliberation.items()
        )
        return max_drift >= self._effective_drift_threshold(current)

    def _effective_drift_threshold(self, snapshot: dict[str, float]) -> float:
        """Dynamic threshold: cautious agents re-deliberate on smaller changes."""
        cautela = snapshot.get("cautela", 0.5)
        return self._drift_threshold - 0.05 * (cautela - 0.5)

    @staticmethod
    def _has_active_goal(context: AgentHookContext) -> bool:
        """Check if there's an active sustained goal in the system prompt metadata."""
        for msg in context.messages:
            if msg.get("role") != "system":
                continue
            content = msg.get("content", "")
            if "Goal (active):" in content or "Goal: active" in content:
                return True
        return False

    async def _run_deliberation(
        self,
        context: AgentHookContext,
        trigger: TriggerReason,
    ) -> None:
        self._last_delib_iteration = context.iteration
        delib_context = self._build_context(context, trigger)
        if self._telemetry:
            self._telemetry.log_deliberation_start(
                trigger=trigger.value,
                goal_summary=delib_context.goal_summary,
                posture_snapshot=delib_context.posture_snapshot,
            )
        t0 = time.perf_counter()
        try:
            self._last_verdict = await self._engine.deliberate(delib_context)
            self._last_synthesis_result = synthesize(
                self._last_verdict, delib_context.posture_snapshot or None,
            )
            self._last_synthesis = render_synthesis(self._last_synthesis_result)
            self._posture_at_last_deliberation = delib_context.posture_snapshot or None
            self._history.append(VerdictEntry(
                timestamp=t0,
                trigger=trigger,
                winner_role=self._last_verdict.winner.proposal.role,
                winner_score=self._last_verdict.winner.final_score,
                threshold=self._last_verdict.threshold,
                under_doubt=self._last_verdict.under_doubt,
                posture_snapshot=dict(delib_context.posture_snapshot),
                synthesis_brief=self._last_synthesis_result.direction[:200],
            ))
            duration_ms = (time.perf_counter() - t0) * 1000
            logger.info(
                "Deliberation [{}]: {} won (score={:.2f}, threshold={:.2f}, rounds={}, doubt={}, {:.0f}ms)",
                trigger.value,
                self._last_verdict.winner.proposal.role,
                self._last_verdict.winner.final_score,
                self._last_verdict.threshold,
                self._last_verdict.rounds_used,
                self._last_verdict.under_doubt,
                duration_ms,
            )
            if self._telemetry:
                self._telemetry.log_deliberation_result(
                    winner_role=self._last_verdict.winner.proposal.role,
                    winner_score=self._last_verdict.winner.final_score,
                    threshold=self._last_verdict.threshold,
                    rounds_used=self._last_verdict.rounds_used,
                    under_doubt=self._last_verdict.under_doubt,
                    all_scores=[
                        {"role": sp.proposal.role, "score": round(sp.final_score, 4)}
                        for sp in self._last_verdict.all_proposals
                    ],
                    duration_ms=duration_ms,
                )
            if context.emit_ui:
                await context.emit_ui("deliberation_result", self._verdict_to_ui())
        except Exception as exc:
            logger.exception("Deliberation failed — proceeding without verdict")
            if self._telemetry:
                self._telemetry.log_deliberation_error(str(exc))

    def _verdict_to_ui(self) -> dict:
        v = self._last_verdict
        if v is None:
            return {}
        return {
            "trigger": v.winner.proposal.role if v.winner else "unknown",
            "winner": {
                "role": v.winner.proposal.role,
                "content": v.winner.proposal.content,
                "score": round(v.winner.final_score, 3),
            } if v.winner else None,
            "proposals": [
                {
                    "role": sp.proposal.role,
                    "content": sp.proposal.content,
                    "score": round(sp.final_score, 3),
                }
                for sp in v.all_proposals
            ],
            "threshold": round(v.threshold, 3),
            "rounds_used": v.rounds_used,
            "under_doubt": v.under_doubt,
            "accepted": v.accepted,
        }

    def _inject_as_pre_message(self, context: AgentHookContext) -> None:
        """Insert deliberation as a system message before the last user message."""
        if not context.messages or not self._last_synthesis:
            return

        # Remove any existing deliberation message
        context.messages[:] = [
            msg for msg in context.messages
            if not (msg.get("role") == "system" and _DELIBERATION_TAG in msg.get("content", ""))
        ]

        # Find last user message and insert before it
        last_user_idx = None
        for i in range(len(context.messages) - 1, -1, -1):
            if context.messages[i].get("role") == "user":
                last_user_idx = i
                break

        deliberation_msg = {
            "role": "system",
            "content": f"{_DELIBERATION_TAG}\n\n{self._last_synthesis}",
        }

        if last_user_idx is not None:
            context.messages.insert(last_user_idx, deliberation_msg)
        else:
            context.messages.append(deliberation_msg)

    def _build_context(
        self,
        context: AgentHookContext,
        trigger: TriggerReason,
    ) -> DeliberationContext:
        goal = self._extract_goal(context)
        recent = self._extract_recent(context)
        snapshot = self._get_posture_snapshot()
        conv_summary = self._extract_conversation_summary(context)
        active_obj = self._extract_active_objective(context)
        prev_verdict = self._format_previous_verdict()

        return DeliberationContext(
            trigger=trigger,
            goal_summary=goal,
            recent_context=recent,
            posture_snapshot=snapshot,
            conversation_summary=conv_summary,
            active_objective=active_obj,
            previous_verdict_brief=prev_verdict,
        )

    def _extract_goal(self, context: AgentHookContext) -> str:
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

    def _extract_recent(self, context: AgentHookContext) -> str:
        tool_names = [tc.name for tc in context.tool_calls]
        if tool_names:
            return f"Tools a ejecutar: {', '.join(tool_names)}"
        return ""

    @staticmethod
    def _extract_conversation_summary(context: AgentHookContext) -> str:
        assistant_msgs = [
            msg for msg in context.messages
            if msg.get("role") == "assistant"
        ]
        recent = assistant_msgs[-5:]
        if not recent:
            return ""
        parts = []
        for msg in recent:
            content = msg.get("content", "")
            if isinstance(content, str) and content.strip():
                parts.append(content.strip()[:100])
        return " | ".join(parts) if parts else ""

    @staticmethod
    def _extract_active_objective(context: AgentHookContext) -> str:
        for msg in context.messages:
            if msg.get("role") != "system":
                continue
            content = msg.get("content", "")
            marker = "Goal (active):"
            idx = content.find(marker)
            if idx == -1:
                continue
            after = content[idx + len(marker):].strip()
            first_line = after.split("\n")[0].strip()
            return first_line[:300]
        return ""

    def _format_previous_verdict(self) -> str:
        last = self._history.last
        if last is None:
            return ""
        return f"{last.winner_role} ({last.winner_score:.2f}): {last.synthesis_brief[:80]}"

    def _get_posture_snapshot(self) -> dict[str, float]:
        if callable(self._posture_snapshot_fn):
            try:
                return self._posture_snapshot_fn()
            except Exception:
                pass
        return {}
