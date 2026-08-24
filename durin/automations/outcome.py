"""What a finished automation run reports, and where that report belongs.

An automation's delivery policy (``durin.automations.classify.should_deliver``)
decides whether a *routine* notice is worth sending, given the automation's
own ``notify``/``silent_labels`` configuration. ``route`` below is the second,
orthogonal decision this module owns: even when that policy says "don't
bother", a run somebody explicitly asked for (a session origin) is always
told, and the automation's ``help`` destination is a backstop for two kinds
of run the delivery lane might otherwise drop entirely — a failure
(``failed``/``interrupted``) so it is never silently lost just because
``notify`` was configured "never", and an achieved goal so a ``help``-only
automation (no ``delivery.channel`` configured) still hears "you're done".

The routing precedence, in order:

1. A session origin ("somebody asked") always wins, regardless of delivery
   policy — a run fired inside a conversation answers inside that
   conversation. A channel/webhook/chain/schedule origin never does: those
   identify what CAUSED the run, not somebody waiting on its answer.
2. Otherwise, if the delivery decision (``should_deliver`` result, or the
   achieved-notice bypass — see ``AutomationsRuntime._post_finish``) says
   this run is notice-worthy AND the automation declares a delivery channel,
   the notice goes there.
3. Otherwise, if the status is actionable (``failed``/``interrupted``) OR
   ``achieved``, and the automation declares a help channel, the notice goes
   there as a backstop. Achieving is the counterpart of escalating: an
   automation configured with only a ``help`` channel (no ``delivery``) is
   exactly the shape ``life.on_stuck``'s escalation notices already use, so
   reaching the goal must be just as audible through that same channel —
   nothing pairs a `help`-only automation with a way to ever hear "you're
   done" otherwise.
4. Otherwise, nowhere — the caller records the run as silenced. Neither
   backstop ever invents a destination: a ``None`` channel means nowhere to
   go, whether that is the delivery channel in step 2 or the help channel in
   step 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from durin.automations.spec import Delivery, Help

# Terminal statuses that deserve a help-channel backstop notice on failure
# grounds, even when the automation's own delivery policy stayed quiet.
# "completed" and "rejected" follow notify policy with no such backstop.
# "achieved" gets its OWN backstop path in route() below (on success grounds,
# not failure grounds) — deliberately kept out of this tuple so a reader
# scanning for "what counts as a failure worth a backstop" isn't misled by an
# achieved run sitting in the same bucket.
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
    # The routed destination, filled in by AutomationsRuntime._deliver_outcome
    # from route()'s result (via dataclasses.replace) right before this value
    # reaches on_outcome — None/None/None on every value build_outcome itself
    # produces, since routing hasn't happened yet at that point. A wiring
    # layer consuming on_outcome must read these three fields verbatim and
    # never recompute routing on its own: re-deriving a destination from a
    # freshly-reloaded spec could disagree with the one record_delivery
    # already persisted for this same run, sending a notice somewhere other
    # than where the run's own delivery record says it went.
    kind: Literal["session", "delivery", "help"] | None = None
    channel: str | None = None  # None for kind == "session"
    to: str | None = None       # None for kind == "session"


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
    # The help channel is a backstop on two distinct grounds: a failure
    # (ACTIONABLE_STATUSES) must never go completely unheard, and an achieved
    # goal must be just as audible on a help-only automation (no delivery
    # channel configured) as an escalation notice already is — achieving is
    # the counterpart of escalating.
    if (outcome.status in ACTIONABLE_STATUSES or outcome.status == "achieved") and help.channel:
        return Destination(kind="help", origin=None, channel=help.channel, to=help.to)
    return None
