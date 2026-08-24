"""What a finished automation run reports, and where that report belongs.

An automation's delivery policy (``durin.automations.classify.should_deliver``)
decides whether a *routine* notice is worth sending, given the automation's
own ``notify``/``silent_labels`` configuration. ``route`` below is the second,
orthogonal decision this module owns: even when that policy says "don't
bother", a run somebody explicitly asked for (a session origin) is always
told, and a run that ends in an actionable status (``failed``/``interrupted``)
always reaches the automation's ``help`` destination as a backstop — so a
failure is never silently lost just because ``notify`` was configured
"never" or the automation has no ``delivery.channel`` at all.

The routing precedence, in order:

1. A session origin ("somebody asked") always wins, regardless of delivery
   policy — a run fired inside a conversation answers inside that
   conversation. A channel/webhook/chain/schedule origin never does: those
   identify what CAUSED the run, not somebody waiting on its answer.
2. Otherwise, if the delivery decision (``should_deliver`` result, or the
   achieved-notice bypass — see ``AutomationsRuntime._post_finish``) says
   this run is notice-worthy AND the automation declares a delivery channel,
   the notice goes there.
3. Otherwise, if the status is actionable and the automation declares a
   help channel, the notice goes there as a backstop.
4. Otherwise, nowhere — the caller records the run as silenced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from durin.automations.spec import Delivery, Help

# Terminal statuses that always deserve a help-channel backstop notice, even
# when the automation's own delivery policy stayed quiet. "achieved",
# "completed", and "rejected" all have their own explicit delivery/routing
# paths (achieved always delivers; completed/rejected follow notify policy)
# and are deliberately absent here.
ACTIONABLE_STATUSES = ("failed", "interrupted")


@dataclass(frozen=True)
class AutomationOutcome:
    automation: str
    run_id: str
    status: str
    summary: str
    origin: dict | None
    workflow_run_id: str | None
    final_route_label: str | None


def build_outcome(automation: str, record: dict) -> AutomationOutcome:
    """Fold a finalized run manifest into the value delivered to a recipient."""
    status = str(record.get("status") or "")
    run_id = str(record.get("run_id") or "")
    wf_run_id = record.get("workflow_run_id")

    parts = [f"Automation '{automation}' run {run_id}: {status}"]
    detail = record.get("detail")
    if detail:
        parts.append(str(detail))

    return AutomationOutcome(
        automation=automation,
        run_id=run_id,
        status=status,
        summary="\n".join(parts),
        origin=record.get("origin") if isinstance(record.get("origin"), dict) else None,
        workflow_run_id=wf_run_id if isinstance(wf_run_id, str) else None,
        final_route_label=record.get("final_route_label"),
    )


@dataclass(frozen=True)
class Destination:
    kind: Literal["session", "delivery", "help"]
    origin: dict | None  # set only for kind == "session"
    channel: str | None  # set only for kind in ("delivery", "help")
    to: str | None       # set only for kind in ("delivery", "help")


def route(outcome: AutomationOutcome, *, deliver: bool, delivery: Delivery, help: Help) -> Destination | None:
    """Pick where a finished run's outcome goes, or None to deliver nowhere.

    ``deliver`` is the caller's already-computed delivery decision
    (``should_deliver`` plus the achieved-notice bypass) — this function does
    not re-derive it, only decides where a "yes" actually goes and whether a
    "no" still needs the actionable-status backstop.
    """
    origin = outcome.origin if isinstance(outcome.origin, dict) else None
    if origin and origin.get("kind") == "session" and origin.get("session_key"):
        return Destination(kind="session", origin=origin, channel=None, to=None)
    if deliver and delivery.channel:
        return Destination(kind="delivery", origin=None, channel=delivery.channel, to=delivery.to)
    if outcome.status in ACTIONABLE_STATUSES and help.channel:
        return Destination(kind="help", origin=None, channel=help.channel, to=help.to)
    return None
