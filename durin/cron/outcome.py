"""What counts as a failed cron agent turn.

The agent loop never raises for a provider failure: it turns the failure into
the reply text ("Sorry, I encountered an error…") and returns it like any
answer, with the runner's ``stop_reason`` riding the outbound metadata. The
cron runner records ``error`` only when its callback raises, so without this
translation a run whose model call never succeeded is filed as ``ok``.
"""

from __future__ import annotations

# Stop reasons of a turn that produced no answer: the provider failed, the
# prompt could not fit the window even after emergency trimming, or the model
# returned nothing after the retry budget. ``max_iterations`` and
# ``tool_error`` are deliberately not here — those turns did work and ended
# with a reply the user can read.
FAILED_TURN_STOP_REASONS: frozenset[str] = frozenset({
    "error",
    "mid_turn_precheck_overflow",
    "empty_final_response",
})


def turn_failed(stop_reason: str | None) -> bool:
    """True when ``stop_reason`` means the turn never produced an answer."""
    return bool(stop_reason) and stop_reason in FAILED_TURN_STOP_REASONS


class CronTurnFailedError(RuntimeError):
    """Raised by the cron callback after a turn that never answered, so the
    run history records ``error`` with the reply text as the reason."""

    def __init__(self, stop_reason: str, reply: str) -> None:
        self.stop_reason = stop_reason
        self.reply = reply
        preview = (reply or "").strip().replace("\n", " ")[:200]
        super().__init__(f"agent turn ended with {stop_reason}: {preview}")
