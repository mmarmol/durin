"""Interpreting a reply to an approval pause, and building the resume state for it.

An approval-flagged work node (``WorkNode.approval``, see ``durin/workflow/spec.py``)
pauses the walk with ``WorkflowResult(status="needs_input", ask_kind="approval")``
instead of threading its output onward (``durin/workflow/engine.py``'s ``_walk``). The
manifest that pause writes is a resume point with three possible replies:

- **approve**: the run continues past the node, unchanged — its proposal becomes the
  upstream input to whatever comes next. If it has nothing next, the run is simply
  done: the proposal IS the final answer (the caller finalizes this directly; this
  module reports it by returning ``None``).
- **reject**: the run ends there. The caller finalizes it directly — this module is
  never consulted for that reply (there is no engine state to build).
- anything else (**revise**): treated as feedback. The SAME node re-runs with the
  reply framed alongside the original upstream, and its approval flag pauses it again
  with the new proposal — the same mechanism as the first pass, not a special case.
"""

from __future__ import annotations

from durin.workflow.engine import ResumeState, manifest_visit_counts
from durin.workflow.spec import Workflow

# Single-word reply vocabulary (case-insensitive, surrounding punctuation stripped).
# Anything else — including a multi-word reply that merely starts with one of these
# words (e.g. "aprobar pero cambia X") — is a revise comment, not a verdict.
_APPROVE_WORDS = {"aprobar", "approve", "ok", "sí", "si", "yes"}
_REJECT_WORDS = {"rechazar", "reject", "no"}
_STRIP_PUNCT = ".,!¡¿?"


def parse_approval_reply(text: str) -> str | None:
    """"approve" or "reject" for a single-word reply naming either vocabulary;
    ``None`` for anything else (a revise comment)."""
    normalized = text.strip().strip(_STRIP_PUNCT).strip().lower()
    if not normalized:
        return None
    if normalized in _APPROVE_WORDS:
        return "approve"
    if normalized in _REJECT_WORDS:
        return "reject"
    return None


def build_approval_resume(
    workflow: Workflow, manifest: dict, action: str, comment: str,
) -> ResumeState | None:
    """The ``ResumeState`` for acting on an approval-pause reply. ``action`` is
    "approve" or "revise" — "reject" never reaches here; the caller finalizes the
    run 'cancelled' directly, without touching the engine.

    "approve": the flagged node does NOT re-run — the walk resumes at its ``next``
    edge with the recorded proposal as upstream. Returns ``None`` when the flagged
    node has no ``next`` (a terminal approval): approving it completes the run
    rather than resuming into anything, so the caller finalizes 'completed' with
    the proposal as ``final_output`` instead of calling back into the engine.

    "revise": the walk resumes AT the flagged node itself, so it re-runs with
    ``comment`` framed as feedback alongside the run's original upstream text.

    ``ResumeState.recorded_outputs`` is left empty except on "approve": the
    resumed walk's own in-memory trace starts empty and never visits the flagged
    node again (it resumes past it), so without seeding the node's own output
    under its id here, a downstream node's ``inputs_from`` reference to it would
    wrongly read as "no output recorded" after the resume. A "revise" resume
    needs no such seeding — the node re-runs and its fresh output lands in the
    resumed walk's own trace like any other node's.
    """
    node_id = manifest["needs_input_node"]
    visits = manifest_visit_counts(manifest)
    work_key = manifest.get("work_key")
    if action == "approve":
        node = workflow.nodes[node_id]
        if node.next is None:
            return None
        proposal = manifest.get("final_output")
        return ResumeState(
            run_id=manifest["run_id"],
            start_at=node.next,
            visits=visits,
            upstream=proposal,
            recorded_outputs={node_id: proposal or ""},
            work_key=work_key,
        )
    original = manifest.get("resume_upstream") or ""
    return ResumeState(
        run_id=manifest["run_id"],
        start_at=node_id,
        visits=visits,
        upstream=f"{original}\n\n[Revision requested by approver]\n{comment}",
        work_key=work_key,
    )
