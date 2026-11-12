"""Telemetry for a run whose process died: its conversion, the resumes that
claim it, the resumes refused and the write-back put back.

No payload holds a path, a process id or task text, and none sets a session
key of its own: only the workflow's name, the run's id and short words (the
logger stamps the session it is bound to on every event). A resume and a
refusal also write one log line each, with the same words; the conversion
writes its own line where it happens. A conversion runs on
the periodic sweep's thread and a refusal in a request, where no session
logger is bound, so ``_emit`` binds one of its own for the call; it leaves a
logger the caller already bound alone, so an event raised inside a live turn
lands in that session's file.
"""

from __future__ import annotations

from contextlib import suppress

from loguru import logger

from durin.workflow.refusal import KINDS

# The session the events are recorded under when the caller has none bound.
_FALLBACK_SESSION = "workflow:resume"


def _emit(event: str, data: dict) -> None:
    """Best-effort: telemetry never breaks a conversion, a resume or a refusal."""
    with suppress(Exception):
        from durin.agent.tools._telemetry import emit_tool_event
        from durin.telemetry.logger import (
            bind_telemetry,
            current_telemetry,
            get_session_logger,
            reset_telemetry,
        )

        token = None
        if current_telemetry() is None:
            token = bind_telemetry(get_session_logger(_FALLBACK_SESSION))
        try:
            emit_tool_event(event, data)
        finally:
            if token is not None:
                reset_telemetry(token)


def found_dead(workflow: str, run_id: str, *, status: str, step: str | None,
               scripts_recorded: int | None) -> None:
    """A run whose process was gone was converted: ``status`` is what it was
    given (``crashed`` or ``cancelled``), ``step`` the step it was in, and
    ``scripts_recorded`` the script records it left beside its record (None
    when they could not be counted)."""
    _emit("workflow.run_found_dead", {
        "workflow": workflow, "run_id": run_id, "status": status, "step": step,
        "scripts_recorded": scripts_recorded})


def resumed(workflow: str, run_id: str, *, kind: str | None, surface: str) -> None:
    """A resume claimed the run. ``kind`` is ``engine.resumable_kind``'s answer."""
    logger.info("run {} of workflow {!r}: resumed ({} run) through the {}",
                run_id, workflow, kind, surface)
    _emit("workflow.run_resumed", {
        "workflow": workflow, "run_id": run_id, "kind": kind, "surface": surface})


def refused(workflow: str, run_id: str, *, surface: str, reason: str) -> None:
    """A resume was refused. ``reason`` is one of ``refusal.KINDS``."""
    word = reason if reason in KINDS else "other"
    logger.info("run {} of workflow {!r}: a resume through the {} was refused ({})",
                run_id, workflow, surface, word)
    _emit("workflow.resume_refused", {
        "workflow": workflow, "run_id": run_id, "surface": surface, "reason": word})


def writeback_restored(workflow: str, run_id: str, *, step: str | None) -> None:
    """A write-back a dead process left cut was put back before ``step``."""
    _emit("workflow.writeback_restored", {
        "workflow": workflow, "run_id": run_id, "step": step})
