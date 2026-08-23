"""Outcome classification and delivery policy.

Pure functions for classifying workflow results into automation outcome states
and determining whether to deliver a notification based on the outcome and
delivery policy.
"""

from __future__ import annotations

from durin.automations.spec import AutomationSpec, Delivery
from durin.workflow.result import WorkflowResult


def is_achieved(result: WorkflowResult, spec: AutomationSpec) -> bool:
    """Determine if a completed result satisfies the automation's achieved condition.

    Returns False when spec.life is None (no achievement condition defined).
    With life defined, checks the achieved_when condition:
    - "any_completed" → True if result.status == "completed"
    - "label:<L>" → True if result.status == "completed" AND result.final_route_label == "<L>"
      (exact case-sensitive match)

    Args:
        result: The workflow result to evaluate.
        spec: The automation spec with the life/achieved_when condition.

    Returns:
        True if the result satisfies the achievement condition, False otherwise.
    """
    if spec.life is None:
        return False

    if spec.life.achieved_when == "any_completed":
        return result.status == "completed"

    if spec.life.achieved_when.startswith("label:"):
        expected_label = spec.life.achieved_when[len("label:"):]
        return result.status == "completed" and result.final_route_label == expected_label

    # Should never reach here if spec is valid (validated at parse time)
    return False


def classify(result: WorkflowResult, spec: AutomationSpec) -> str:
    """Classify a workflow result into an automation outcome state.

    Returns one of: "paused", "achieved", "completed", "rejected", "failed"

    Classification rules (in precedence order):
    1. result.status == "needs_input" → "paused"
    2. result.rejected truthy → "rejected" (takes precedence over other statuses)
    3. result.status == "completed" and is_achieved(result, spec) → "achieved"
    4. result.status == "completed" → "completed"
    5. Anything else (exhausted, aborted, cancelled) → "failed"

    Args:
        result: The workflow result to classify.
        spec: The automation spec with life/achieved_when condition.

    Returns:
        The outcome classification as a string.
    """
    if result.status == "needs_input":
        return "paused"

    if result.rejected:
        return "rejected"

    if result.status == "completed":
        if is_achieved(result, spec):
            return "achieved"
        return "completed"

    return "failed"


def should_deliver(status: str, final_route_label: str | None, delivery: Delivery) -> bool:
    """Determine whether a notification should be delivered for an outcome.

    Classification rules by delivery.notify mode:
    - "never" → False for all statuses (note: caller handles the achieved-notice exception)
    - "always" → True for all statuses
    - "failures_only" → True only for "failed" status; False for all others
    - "when_notable" → True when:
      - status == "failed", OR
      - status in ("completed", "achieved") AND final_route_label NOT in delivery.silent_labels
      False for: "paused", "rejected", "interrupted" and completed/achieved with silent labels

    Note: The caller enforces the achieved-notice delivery exception — this function
    returns False for achieved when the label is silent, and the caller handles
    overriding that to always deliver for achieved outcomes. This function also
    returns False for "paused" (which routes via help lane instead), "rejected"
    (governed separately), and "interrupted" (routes via outcome backstop).

    Args:
        status: The outcome status string.
        final_route_label: The final route label if any (None if no routing occurred).
        delivery: The delivery policy with notify mode and silent labels.

    Returns:
        True if a notification should be delivered, False otherwise.
    """
    if delivery.notify == "never":
        return False

    if delivery.notify == "always":
        return True

    if delivery.notify == "failures_only":
        return status == "failed"

    if delivery.notify == "when_notable":
        if status == "failed":
            return True
        if status in ("completed", "achieved"):
            return final_route_label not in delivery.silent_labels
        # paused, rejected, interrupted: no delivery
        return False

    # Should never reach here if delivery is valid
    return False
