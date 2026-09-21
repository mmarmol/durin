"""Which turn outcomes count as a failed cron run."""

from __future__ import annotations

import pytest

from durin.cron.outcome import CronTurnFailedError, turn_failed


@pytest.mark.parametrize(
    "stop_reason",
    ["error", "mid_turn_precheck_overflow", "empty_final_response"],
)
def test_turn_that_never_answered_is_a_failed_run(stop_reason: str) -> None:
    assert turn_failed(stop_reason) is True


@pytest.mark.parametrize("stop_reason", ["stop", "max_iterations", "tool_error", None, ""])
def test_turn_that_produced_an_answer_is_not_a_failed_run(stop_reason: str | None) -> None:
    assert turn_failed(stop_reason) is False


def test_failed_turn_error_names_the_reason_and_the_reply() -> None:
    exc = CronTurnFailedError("error", "Sorry, I encountered an error calling the AI model.")
    assert "error" in str(exc)
    assert "Sorry, I encountered an error" in str(exc)
    assert exc.stop_reason == "error"
