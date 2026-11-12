"""One resume of a run at a time, whichever surface resumes it.

A stopped run is resumable while its record says so: ``needs_input`` (paused
on a question or an approval), ``aborted`` with a ``failed_node`` (a step
failed) or ``crashed`` with a position (its process died; see
``engine.resumable_kind``). Two callers can read that same record at once —
the webui, the API, an automation's answer, the agent's ``run_workflow``
tool, a cancel — and each would go on to act on it. A resume therefore *claims* the record before
it starts: under the per-run lock it re-reads the record, refuses unless it
is still exactly the one the resume accepted, and moves it to ``running``
owned by this process. A racing resume or cancel then reads ``running`` under
the same lock and refuses.

The claim stands until the engine writes the run's own record. When that
never happens — the resume is dropped while it waits for a workflow-run
thread, or the engine returns or raises before its walk (a rejected input or
script, a busy ``work_key``) — the claim is released and the record put back
exactly as it was: nothing else would ever end a ``running`` record owned by
this live process, and the run could be neither resumed nor cancelled again.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

from loguru import logger

from durin.utils.file_lock import cross_process_lock
from durin.workflow import run_log

__all__ = ["ResumeClaim", "ResumeLostError"]

_T = TypeVar("_T")


class ResumeLostError(RuntimeError):
    """The record changed between the caller's read and its claim: someone else
    resumed, cancelled or finalized the run first."""


class ResumeClaim:
    """A taken claim on one run's record, from ``take`` until the engine writes
    the record or the claim is released."""

    def __init__(self, workspace: str | Path, name: str, run_id: str, *,
                 prior: dict, claimed: dict) -> None:
        self._workspace = workspace
        self._name = name
        self._run_id = run_id
        self._prior = prior
        self._claimed = claimed
        # The run's thread (starting the walk) and a cancelled caller (giving
        # up on it) can race; whichever takes this lock first decides which
        # one of them releases the claim.
        self._handoff = threading.Lock()
        self._started = False
        self._dropped = False
        # A claim is released at most once. A later claim of the same record by
        # this process writes exactly the record this one wrote, so a second
        # release could not tell them apart and would undo that later claim.
        self._released = False
        # Whether the release put the record back because the engine never
        # wrote the run's own record.
        self._put_back = False

    @classmethod
    def take(cls, workspace: str | Path, name: str, run_id: str, *,
             expected: dict) -> ResumeClaim:
        """Under the per-run lock: re-read the record, raise ResumeLostError unless
        it still equals `expected`, then move it to `running` owned by this process."""
        with cross_process_lock(run_log.run_lock_target(workspace, name, run_id)):
            if run_log.read_manifest(workspace, name, run_id) != expected:
                raise ResumeLostError(
                    f"run {run_id!r} of workflow {name!r} cannot be resumed — it was "
                    "cancelled, or already resumed, before this reached it.")
            claimed = run_log.claim_for_resume(workspace, name, run_id)
        return cls(workspace, name, run_id, prior=expected, claimed=claimed)

    def guard(self, walk: Callable[..., _T]) -> Callable[..., _T | None]:
        """Wrap the function that runs the engine. The wrapper marks the walk as
        started and, when it returns or raises, puts the record back as it was
        if the engine never wrote the run's own record. If `drop()` won the
        race, the wrapper returns None without calling `walk`."""
        def _guarded(*args: Any, **kwargs: Any) -> _T | None:
            with self._handoff:
                if self._dropped:
                    return None   # the caller is gone and released the claim
                self._started = True
            try:
                return walk(*args, **kwargs)
            finally:
                self._release()
        return _guarded

    @property
    def took_over(self) -> bool:
        """Whether the engine took the run over: the walk started and the engine
        wrote the run's own record, so the record was not put back. Meaningful
        once the guarded walk has returned or raised; False for a claim dropped
        before its walk, or released because the engine ended before writing
        the record (a rejected input or script, a busy ``work_key``)."""
        return self._started and not self._put_back

    def drop(self) -> None:
        """The caller is giving up before the walk started (cancelled while
        waiting for a thread, or before its task ran at all): put the record
        back. No effect once the walk has started, or on a second call."""
        with self._handoff:
            self._dropped = not self._started
        if self._dropped:
            self._release()

    def _release(self) -> None:
        # Does nothing once the engine rewrote the record (it then differs from
        # the claimed one), or once this claim was released already. A failure
        # here is logged and never replaces the run's own outcome or the
        # caller's exception.
        with self._handoff:
            if self._released:
                return
            self._released = True
        try:
            with cross_process_lock(run_log.run_lock_target(self._workspace, self._name, self._run_id)):
                self._put_back = run_log.release_resume_claim(
                    self._workspace, self._name, self._run_id,
                    prior=self._prior, claimed=self._claimed)
        except Exception:  # noqa: BLE001 - never masks the run's own outcome
            logger.exception("releasing the resume claim on run {} of workflow {!r} failed",
                             self._run_id, self._name)
