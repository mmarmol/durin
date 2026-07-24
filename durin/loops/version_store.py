"""Internal git versioning of loop definitions.

A workspace's ``loops/`` directory is kept as a small local git repo, the same
mechanism workflows, skills and memory use. Loops were the last editable surface
with no history: a change from the webui, the agent tool or the CLI overwrote the
previous definition with nothing to review or roll back to.

Commits are serialized with a cross-process lock kept beside the loops dir, and
versioning is strictly best-effort: a failure logs and returns, it must never
break a save — the definition itself already landed.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from durin.utils.file_lock import cross_process_lock
from durin.utils.git_repo import GitRepo, NothingToCommitError

# Lock target name, kept beside the loops dir (not inside it) so the ".lock" file
# cross_process_lock derives never lands in a versioned commit. Distinct from the
# per-loop write locks, which stay inside loops/ and are gitignored instead —
# those serialize same-name writers, this one serializes git.
VERSION_LOCK_NAME = ".loop-version"

# The per-loop write locks live inside loops/ by design (a lock per name, so
# writers on different loops never block each other), so the repo must ignore
# them or every commit would carry a lock artifact.
_GITIGNORE = ["*.lock"]


def version_lock_target(loops_dir: str | Path) -> Path:
    """The cross-process lock target serializing commits of ``loops_dir``."""
    return Path(loops_dir).parent / VERSION_LOCK_NAME


class LoopVersionStore:
    def __init__(self, loops_dir: str | Path) -> None:
        self.dir = Path(loops_dir)
        self._repo = GitRepo(
            self.dir, default_author="durin-loop", default_email="loop@durin.local"
        )
        self._lock = version_lock_target(self.dir)

    def commit_paths(
        self,
        paths: list[Path],
        subject: str,
        reason: str,
        *,
        actor: str,
    ) -> str | None:
        """Commit the touched paths with ``Reason`` and ``Actor`` trailers.

        Deletions are staged by passing the (now missing) path. Best-effort,
        locked, never raises — returns the SHA, or None when nothing changed.
        """
        try:
            if not self.dir.is_dir():
                return None
            with cross_process_lock(self._lock):
                if not self._repo.is_initialized():
                    self._repo.init(gitignore_patterns=_GITIGNORE)
                try:
                    return self._repo.commit(
                        subject=subject,
                        trailers={"Reason": reason, "Actor": actor},
                        paths=list(paths),
                    )
                except NothingToCommitError:
                    return None
        except Exception:  # noqa: BLE001 - versioning must never break a save
            logger.exception("loop version commit failed for {}", subject)
            return None

    def history(self, name: str | None = None, *, limit: int = 20):
        """Recent versions newest-first. With ``name``, scopes to commits since
        ``<name>.json`` existed (the underlying log is presence-based, not a
        per-commit change filter), so callers diff consecutive versions."""
        try:
            if not self._repo.is_initialized():
                return []
            path = (self.dir / f"{name}.json") if name else None
            return self._repo.log(path, max_count=limit)
        except Exception:  # noqa: BLE001
            logger.exception("loop version history failed for {}", self.dir)
            return []
