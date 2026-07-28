"""What a finished loop run reports, and where that report belongs.

A loop promises that its goal is reached or a human finds out. The payload
here is the "finds out" half: one value per terminal run, carrying enough for
a recipient to know what happened and what handle to act on. Routing lives
beside it because the decision is pure — it reads the run's recorded origin
and the loop's declared destination and picks one, with no I/O.

The recipient is always somebody on durin's side: the session that asked, or
the operator. An outcome is never posted back to the counterpart a channel
origin identifies — that party is being corresponded with, not reported to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Terminal statuses worth interrupting somebody who did not ask for this run.
# `done` is deliberately absent: a scheduled loop meeting its goal is the
# normal case, and reporting it to a standing destination is noise. A run
# somebody explicitly requested is delivered regardless of status.
ACTIONABLE_STATUSES = ("no_goal", "error", "escalated", "interrupted")


@dataclass(frozen=True)
class LoopOutcome:
    loop: str
    run_id: str
    status: str
    goal_reached: bool | None
    summary: str
    origin: dict | None
    workflow_run_id: str | None


def build_outcome(loop: str, record: dict) -> LoopOutcome:
    """Fold a finalized run manifest into the value delivered to a recipient."""
    status = str(record.get("status") or "")
    run_id = str(record.get("run_id") or "")
    wf_run_id = record.get("workflow_run_id")

    parts = [f"Loop '{loop}' run {run_id}: {status}"]
    detail = record.get("detail")
    if detail:
        parts.append(str(detail))
    # Only claim partial work when sweep_orphans found the workflow's own
    # manifest already on disk (`work_started`, persisted on the record —
    # see run_log.finalize_run). workflow_run_id alone is not evidence: `_run`
    # mints and persists it before execute() ever starts, so it is present
    # even for a run that was killed before the workflow took a single step.
    if status == "interrupted" and wf_run_id and record.get("work_started"):
        parts.append(f"Workflow run {wf_run_id} may hold partial work.")

    return LoopOutcome(
        loop=loop,
        run_id=run_id,
        status=status,
        goal_reached=record.get("goal_reached"),
        summary="\n".join(parts),
        origin=record.get("origin"),
        workflow_run_id=wf_run_id if isinstance(wf_run_id, str) else None,
    )


@dataclass(frozen=True)
class Destination:
    kind: Literal["session", "operator"]
    origin: dict | None


def route(outcome: LoopOutcome, *, operator_channel: str | None) -> Destination | None:
    """Pick where an outcome goes, or None when it should not be delivered.

    An agent session is the only origin that counts as "somebody asked": a
    run fired inside a conversation answers inside that conversation.

    A channel origin does NOT. It identifies the counterpart — the external
    party the loop is corresponding with — not a requester: an inbound event
    fired that run, nobody on durin's side did. An outcome is internal status,
    and internal status must never reach the counterpart, whose lane carries
    only workflow-authored prose. So a channel origin falls through to the
    loop's declared channel exactly like a cron fire does.

    That declared channel is a backstop, not an override, and only hears about
    outcomes worth interrupting somebody who did not ask for this run.
    """
    origin = outcome.origin if isinstance(outcome.origin, dict) else None
    if origin and origin.get("kind") == "session" and origin.get("session_key"):
        return Destination(kind="session", origin=origin)
    if operator_channel and outcome.status in ACTIONABLE_STATUSES:
        return Destination(kind="operator", origin=None)
    return None
