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

# Lock target name, kept beside the workflows dir (not inside it) so the ".lock" file
# cross_process_lock derives never lands in a versioned snapshot. Shared with the HTTP
# save/delete path so an editor write and a snapshot commit never interleave.
VERSION_LOCK_NAME = ".workflow-version"


def version_lock_target(workflows_dir: str | Path) -> Path:
    """The cross-process lock target serializing edits and snapshots of ``workflows_dir``."""
    return Path(workflows_dir).parent / VERSION_LOCK_NAME


class WorkflowVersionStore:
    def __init__(self, workflows_dir: str | Path) -> None:
        self.dir = Path(workflows_dir)
        self._repo = GitRepo(
            self.dir, default_author="durin-workflow", default_email="workflow@durin.local"
        )
        self._lock = version_lock_target(self.dir)

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

    def commit_paths(
        self,
        paths: list[Path],
        subject: str,
        reason: str,
        *,
        actor: str,
    ) -> str | None:
        """Commit an arbitrary set of touched paths as ONE version.

        A rename is three mutations — the new definition, the removal of the old
        one, and the repointed sub-flow reference in every caller — and they have
        to land together, or the history shows an intermediate state where a
        caller points at a workflow that does not exist. Deletions are staged by
        passing the (now missing) path: staging a removed path records it.

        Best-effort, locked, never raises — same contract as the rest of the store.
        """
        try:
            with cross_process_lock(self._lock):
                if not self._repo.is_initialized():
                    self._repo.init()
                try:
                    return self._repo.commit(
                        subject=subject,
                        trailers={"Reason": reason, "Actor": actor},
                        paths=list(paths),
                    )
                except NothingToCommitError:
                    return None
        except Exception:  # noqa: BLE001
            logger.exception("workflow version commit failed for {}", subject)
            return None

    def commit_edit(self, name: str, reason: str, *, actor: str = "dream") -> str | None:
        """Commit the current ``<name>.json`` with a ``Reason`` trailer (the rationale
        for an edit) and an ``Actor`` trailer. This is what an applied dream edit uses,
        so the change history records *why* — and the git-history guard reads it back to
        avoid re-proposing reverted edits. Best-effort, locked, never raises."""
        try:
            with cross_process_lock(self._lock):
                if not self._repo.is_initialized():
                    self._repo.init()
                try:
                    return self._repo.commit(
                        subject=f"workflow({name}): edit",
                        trailers={"Reason": reason, "Actor": actor},
                        paths=[self.dir / f"{name}.json"],
                    )
                except NothingToCommitError:
                    return None
        except Exception:  # noqa: BLE001
            logger.exception("workflow edit commit failed for {}", name)
            return None


def history_for_dream(workspace, name: str, *, limit: int = 10) -> list[dict]:
    """Shape a workflow's change history for a dream proposal pass: newest-first, each
    entry the commit's reason plus the diff vs the previous version. Lets dream see
    what was already tried (and reverted) so it doesn't re-propose it."""
    from durin.workflow.loader import workflows_dir

    store = WorkflowVersionStore(workflows_dir(workspace))
    commits = store.history(name, limit=limit)
    path = workflows_dir(workspace) / f"{name}.json"
    out: list[dict] = []
    for i, c in enumerate(commits):
        raw = c.trailers.get("Reason") if c.trailers else None
        reason = (raw[0] if isinstance(raw, list) and raw else raw) or "—"
        diff = ""
        if i + 1 < len(commits):
            try:
                diff = store._repo.diff(from_sha=commits[i + 1].sha, to_sha=c.sha, path=path)
            except Exception:  # noqa: BLE001
                diff = ""
        out.append({"sha": c.sha[:8], "subject": c.subject, "reason": reason, "diff": diff})
    return out
