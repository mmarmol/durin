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

    ``ResumeState.recorded_outputs`` always starts from the manifest's own
    ``resume_inputs`` (the same seed ``build_resume_state`` uses) — the pause
    already recorded it, for the SAME reason any other needs_input/aborted pause
    does (``run_log._resume_inputs``): the resumed walk's own in-memory trace
    starts empty, so any node's ``inputs_from`` reference to a source that ran
    BEFORE the pause (and is not about to run again this pass) has nowhere else to
    resolve from. On top of that seed, "approve" overlays the flagged node's own
    output (the proposal) under its id: on that path the resumed walk starts at
    ``next`` and never revisits the flagged node at all (it resumes past it), so
    a downstream ``inputs_from`` reference to it would otherwise read as "no
    output recorded" even though it very much ran. The overlay wins over whatever
    ``resume_inputs`` may already hold for that id, since the proposal is the
    freshest, most authoritative value. "revise" needs no such overlay — the node
    re-runs and its fresh output lands in the resumed walk's own trace like any
    other node's.
    """
    node_id = manifest["needs_input_node"]
    visits = manifest_visit_counts(manifest)
    work_key = manifest.get("work_key")
    recorded_outputs = dict(manifest.get("resume_inputs") or {})
    if action == "approve":
        node = workflow.nodes[node_id]
        if node.next is None:
            return None
        proposal = manifest.get("final_output")
        recorded_outputs[node_id] = proposal or ""
        return ResumeState(
            run_id=manifest["run_id"],
            start_at=node.next,
            visits=visits,
            upstream=proposal,
            recorded_outputs=recorded_outputs,
            work_key=work_key,
        )
    original = manifest.get("resume_upstream") or ""
    return ResumeState(
        run_id=manifest["run_id"],
        start_at=node_id,
        visits=visits,
        upstream=f"{original}\n\n[Revision requested by approver]\n{comment}",
        recorded_outputs=recorded_outputs,
        work_key=work_key,
    )
