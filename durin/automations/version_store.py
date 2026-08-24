"""Internal git versioning of automation definitions.

A workspace's ``automations/`` directory is kept as a small local git repo,
the same mechanism loops, workflows, skills and memory use. A change from the
webui, the agent tool or the CLI overwrites the previous definition with
nothing to review or roll back to otherwise.

Commits are serialized with a cross-process lock kept beside the automations
dir, and versioning is strictly best-effort: a failure logs and returns, it
must never break a save — the definition itself already landed.
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from durin.utils.file_lock import cross_process_lock
from durin.utils.git_repo import GitRepo, NothingToCommitError

# Lock target name, kept beside the automations dir (not inside it) so the
# ".lock" file cross_process_lock derives never lands in a versioned commit.
# Distinct from the per-automation write locks, which stay inside
# automations/ and are gitignored instead — those serialize same-name
# writers, this one serializes git.
VERSION_LOCK_NAME = ".automation-version"

# The per-automation write locks live inside automations/ by design (a lock
# per name, so writers on different automations never block each other), so
# the repo must ignore them or every commit would carry a lock artifact.
_GITIGNORE = ["*.lock"]


def version_lock_target(automations_dir: str | Path) -> Path:
    """The cross-process lock target serializing commits of ``automations_dir``."""
    return Path(automations_dir).parent / VERSION_LOCK_NAME


class AutomationVersionStore:
    def __init__(self, automations_dir: str | Path) -> None:
        self.dir = Path(automations_dir)
        self._repo = GitRepo(
            self.dir, default_author="durin-automation", default_email="automation@durin.local"
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
            logger.exception("automation version commit failed for {}", subject)
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
            logger.exception("automation version history failed for {}", self.dir)
            return []
