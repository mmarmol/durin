"""What a finished loop run reports, and where that report belongs.

A loop promises that its goal is reached or a human finds out. The payload
here is the "finds out" half: one value per terminal run, carrying enough for
a recipient to know what happened and what handle to act on. Routing lives
beside it because the decision is pure — it reads the run's recorded origin
and the loop's declared destination and picks one, with no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    if status == "interrupted" and wf_run_id:
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
