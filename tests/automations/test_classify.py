"""Tests for outcome classification and delivery policy.

Table-driven tests covering every classification and delivery rule.
"""

import pytest

from durin.automations.spec import AutomationSpec, Delivery, Life
from durin.automations.classify import classify, is_achieved, should_deliver
from durin.workflow.result import WorkflowResult


class TestClassify:
    """Tests for classify(result, spec) -> str"""

    @pytest.mark.parametrize(
        "status,rejected,life,final_route_label,expected",
        [
            # Rule 1: needs_input → "paused"
            ("needs_input", False, None, None, "paused"),
            ("needs_input", False, Life(intent="test"), None, "paused"),
            ("needs_input", False, Life(intent="test", achieved_when="any_completed"), None, "paused"),

            # Rule 2: rejected (truthy) → "rejected" (precedence over other statuses)
            # rejected can ride on "cancelled" status when approval is denied
            ("cancelled", True, None, None, "rejected"),
            ("cancelled", True, Life(intent="test"), None, "rejected"),

            # Rule 3: completed + is_achieved → "achieved"
            # 3a: achieved_when="any_completed"
            ("completed", False, Life(intent="test", achieved_when="any_completed"), None, "achieved"),
            ("completed", False, Life(intent="test", achieved_when="any_completed"), "COBRADA", "achieved"),

            # 3b: achieved_when="label:<LABEL>" with matching final_route_label
            ("completed", False, Life(intent="test", achieved_when="label:COBRADA"), "COBRADA", "achieved"),
            ("completed", False, Life(intent="test", achieved_when="label:SUCCESS"), "SUCCESS", "achieved"),

            # Rule 4: completed but not achieved → "completed"
            ("completed", False, None, None, "completed"),
            ("completed", False, None, "COBRADA", "completed"),
            # achieved_when="label:X" but final_route_label doesn't match
            ("completed", False, Life(intent="test", achieved_when="label:COBRADA"), "FAIL", "completed"),
            ("completed", False, Life(intent="test", achieved_when="label:COBRADA"), None, "completed"),

            # Rule 5: terminal statuses (exhausted, aborted, cancelled) → "failed"
            # unless rejected=True (which takes precedence)
            ("exhausted", False, None, None, "failed"),
            ("aborted", False, None, None, "failed"),
            ("cancelled", False, None, None, "failed"),
            ("exhausted", False, Life(intent="test"), None, "failed"),
            ("aborted", False, Life(intent="test"), None, "failed"),
            ("cancelled", False, Life(intent="test"), None, "failed"),
        ],
        ids=[
            "needs_input_no_life",
            "needs_input_with_life",
            "needs_input_with_any_completed",
            "rejected_on_cancelled",
            "rejected_on_cancelled_with_life",
            "achieved_any_completed_no_label",
            "achieved_any_completed_with_label",
            "achieved_label_cobrada_match",
            "achieved_label_success_match",
            "completed_no_life",
            "completed_with_unmatched_label",
            "completed_label_mismatch",
            "completed_label_none_when_label_required",
            "exhausted_to_failed",
            "aborted_to_failed",
            "cancelled_to_failed",
            "exhausted_with_life_to_failed",
            "aborted_with_life_to_failed",
            "cancelled_without_rejected_to_failed",
        ],
    )
    def test_classify(self, status, rejected, life, final_route_label, expected):
        result = WorkflowResult(
            status=status,  # type: ignore
            final_output="test output",
            final_route_label=final_route_label,
            rejected=rejected,
        )
        spec = AutomationSpec(name="test", workflow="test_wf", life=life)
        assert classify(result, spec) == expected


class TestIsAchieved:
    """Tests for is_achieved(result, spec) -> bool"""

    @pytest.mark.parametrize(
        "life,final_route_label,expected",
        [
            # No life → False
            (None, None, False),
            (None, "COBRADA", False),

            # achieved_when="any_completed" → True for any completed
            (Life(intent="test", achieved_when="any_completed"), None, True),
            (Life(intent="test", achieved_when="any_completed"), "COBRADA", True),
            (Life(intent="test", achieved_when="any_completed"), "FAIL", True),

            # achieved_when="label:X" → True only if final_route_label == "X" (case-sensitive)
            (Life(intent="test", achieved_when="label:COBRADA"), "COBRADA", True),
            (Life(intent="test", achieved_when="label:COBRADA"), "cobrada", False),  # Case mismatch
            (Life(intent="test", achieved_when="label:COBRADA"), "FAIL", False),
            (Life(intent="test", achieved_when="label:COBRADA"), None, False),
            (Life(intent="test", achieved_when="label:SUCCESS"), "SUCCESS", True),
            (Life(intent="test", achieved_when="label:SUCCESS"), "FAIL", False),
        ],
        ids=[
            "no_life_none",
            "no_life_with_label",
            "any_completed_none",
            "any_completed_with_cobrada",
            "any_completed_with_fail",
            "label_cobrada_match",
            "label_cobrada_case_mismatch",
            "label_cobrada_mismatch",
            "label_cobrada_none",
            "label_success_match",
            "label_success_mismatch",
        ],
    )
    def test_is_achieved(self, life, final_route_label, expected):
        result = WorkflowResult(
            status="completed",
            final_output="test",
            final_route_label=final_route_label,
        )
        spec = AutomationSpec(name="test", workflow="test_wf", life=life)
        assert is_achieved(result, spec) == expected


class TestShouldDeliver:
    """Tests for should_deliver(status, final_route_label, delivery) -> bool"""

    @pytest.mark.parametrize(
        "notify_mode,status,final_route_label,silent_labels,expected",
        [
            # "never" → False for all statuses (even achieved — caller handles the exception)
            ("never", "failed", None, ("NOTHING_TO_REPORT",), False),
            ("never", "completed", None, ("NOTHING_TO_REPORT",), False),
            ("never", "achieved", None, ("NOTHING_TO_REPORT",), False),
            ("never", "paused", None, ("NOTHING_TO_REPORT",), False),
            ("never", "rejected", None, ("NOTHING_TO_REPORT",), False),

            # "always" → True for all statuses
            ("always", "failed", None, ("NOTHING_TO_REPORT",), True),
            ("always", "completed", None, ("NOTHING_TO_REPORT",), True),
            ("always", "achieved", None, ("NOTHING_TO_REPORT",), True),
            ("always", "paused", None, ("NOTHING_TO_REPORT",), True),
            ("always", "rejected", None, ("NOTHING_TO_REPORT",), True),

            # "failures_only" → True only for "failed"
            ("failures_only", "failed", None, ("NOTHING_TO_REPORT",), True),
            ("failures_only", "completed", None, ("NOTHING_TO_REPORT",), False),
            ("failures_only", "achieved", None, ("NOTHING_TO_REPORT",), False),
            ("failures_only", "paused", None, ("NOTHING_TO_REPORT",), False),
            ("failures_only", "rejected", None, ("NOTHING_TO_REPORT",), False),

            # "when_notable" → True for "failed" or (completed/achieved with label not in silent_labels)
            # "failed" always delivers
            ("when_notable", "failed", None, ("NOTHING_TO_REPORT",), True),
            ("when_notable", "failed", "COBRADA", ("NOTHING_TO_REPORT",), True),

            # "completed" with label not in silent_labels → True
            ("when_notable", "completed", "COBRADA", ("NOTHING_TO_REPORT",), True),
            ("when_notable", "completed", "COBRADA", ("NOTHING_TO_REPORT", "COBRADA"), False),
            ("when_notable", "completed", "NOTHING_TO_REPORT", ("NOTHING_TO_REPORT",), False),
            # "completed" with None label (no silent_labels contains None) → True
            ("when_notable", "completed", None, ("NOTHING_TO_REPORT",), True),

            # "achieved" with label not in silent_labels → True
            ("when_notable", "achieved", "COBRADA", ("NOTHING_TO_REPORT",), True),
            ("when_notable", "achieved", "COBRADA", ("NOTHING_TO_REPORT", "COBRADA"), False),
            ("when_notable", "achieved", "NOTHING_TO_REPORT", ("NOTHING_TO_REPORT",), False),
            # "achieved" with None label → True
            ("when_notable", "achieved", None, ("NOTHING_TO_REPORT",), True),

            # "paused", "rejected", "interrupted" under "when_notable" → False
            ("when_notable", "paused", None, ("NOTHING_TO_REPORT",), False),
            ("when_notable", "rejected", None, ("NOTHING_TO_REPORT",), False),
            ("when_notable", "interrupted", None, ("NOTHING_TO_REPORT",), False),

            # "paused", "rejected", "interrupted" under "failures_only" → False
            ("failures_only", "paused", None, ("NOTHING_TO_REPORT",), False),
            ("failures_only", "rejected", None, ("NOTHING_TO_REPORT",), False),
            ("failures_only", "interrupted", None, ("NOTHING_TO_REPORT",), False),

            # Multiple silent labels
            ("when_notable", "completed", "COBRADA", ("NOTHING_TO_REPORT", "COBRADA", "SKIP_ME"), False),
            ("when_notable", "completed", "SKIP_ME", ("NOTHING_TO_REPORT", "COBRADA", "SKIP_ME"), False),
            ("when_notable", "completed", "UNEXPECTED", ("NOTHING_TO_REPORT", "COBRADA", "SKIP_ME"), True),
        ],
        ids=[
            "never_failed",
            "never_completed",
            "never_achieved",
            "never_paused",
            "never_rejected",
            "always_failed",
            "always_completed",
            "always_achieved",
            "always_paused",
            "always_rejected",
            "failures_only_failed",
            "failures_only_completed",
            "failures_only_achieved",
            "failures_only_paused",
            "failures_only_rejected",
            "when_notable_failed_no_label",
            "when_notable_failed_with_label",
            "when_notable_completed_cobrada_not_silent",
            "when_notable_completed_cobrada_silent",
            "when_notable_completed_nothing_silent",
            "when_notable_completed_none_label",
            "when_notable_achieved_cobrada_not_silent",
            "when_notable_achieved_cobrada_silent",
            "when_notable_achieved_nothing_silent",
            "when_notable_achieved_none_label",
            "when_notable_paused",
            "when_notable_rejected",
            "when_notable_interrupted",
            "failures_only_paused",
            "failures_only_rejected",
            "failures_only_interrupted",
            "when_notable_multi_silent_cobrada",
            "when_notable_multi_silent_skip_me",
            "when_notable_multi_silent_unexpected",
        ],
    )
    def test_should_deliver(self, notify_mode, status, final_route_label, silent_labels, expected):
        delivery = Delivery(notify=notify_mode, silent_labels=tuple(silent_labels))  # type: ignore
        assert should_deliver(status, final_route_label, delivery) == expected
