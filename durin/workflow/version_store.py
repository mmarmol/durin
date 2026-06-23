"""Internal git versioning of workflow definitions.

A workspace's ``workflows/`` directory is kept as a small local git repo (via the
shared dulwich-backed :class:`GitRepo`, the same mechanism memory and skills use).
Every run snapshots the current definitions, so there is a navigable history of how
each workflow changed and which version a run used — the substrate a later
self-improvement pass can read to avoid re-proposing reverted edits, and a future UI
can show as a timeline.

Commits are serialized with a cross-process lock so concurrent runs (gateway + cron)
don't race the git index. Versioning is strictly best-effort: a failure here logs and
returns, it must never break a workflow run.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from durin.utils.file_lock import cross_process_lock
from durin.utils.git_repo import GitRepo, NothingToCommitError


class WorkflowVersionStore:
    def __init__(self, workflows_dir: str | Path) -> None:
        self.dir = Path(workflows_dir)
        self._repo = GitRepo(
            self.dir, default_author="durin-workflow", default_email="workflow@durin.local"
        )
        # Lock file lives beside the workflows dir, not inside it, so it never lands
        # in a snapshot.
        self._lock = self.dir.parent / ".workflow-version.lock"

    def snapshot(self, reason: str) -> str | None:
        """Commit the current workflow definitions if they changed.

        Returns the new commit SHA, or ``None`` when nothing changed or on any error.
        Best-effort and locked — never raises.
        """
        try:
            if not self.dir.is_dir():
                return None
            with cross_process_lock(self._lock):
                if not self._repo.is_initialized():
                    self._repo.init()
                try:
                    return self._repo.commit(subject=reason)
                except NothingToCommitError:
                    return None
        except Exception:  # noqa: BLE001 - versioning must not break a run
            logger.exception("workflow version snapshot failed for {}", self.dir)
            return None

    def history(self, name: str | None = None, *, limit: int = 20):
        """Recent versions newest-first. With ``name``, scopes to commits since
        ``<name>.json`` existed (the underlying log is presence-based, not a
        per-commit change filter), so callers diff consecutive versions to see edits."""
        try:
            if not self._repo.is_initialized():
                return []
            path = (self.dir / f"{name}.json") if name else None
            return self._repo.log(path, max_count=limit)
        except Exception:  # noqa: BLE001
            logger.exception("workflow version history failed for {}", self.dir)
            return []
