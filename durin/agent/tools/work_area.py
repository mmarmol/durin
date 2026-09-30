"""Per-session work area: where the agent's free-form files land.

The agent's relative-path base is ``work/<session>/`` so new files it creates
stay out of the workspace root (which holds managed surfaces). A fixed set of
managed top-level names still resolves against the workspace root, so file
references and ingested drill-paths keep working. The rule is applied per path
string, so a given relative path means one location for both read and write.
"""
from __future__ import annotations

from pathlib import Path

from durin.session.manager import SessionManager

__all__ = ["MANAGED_PREFIXES", "session_work_dir", "anchored_base", "read_base", "display_path"]

# Top-level names that resolve against the workspace root rather than the
# session work dir. These are durin-managed surfaces the agent reads by name
# (file references, ingested drill-paths). ``work`` itself is anchored so that
# completer-offered ``work/<session>/...`` paths resolve correctly too.
# ``skill-drafts`` sits next to ``skills``: the draft scratch area a skill is
# built and tested in before ``skill_publish`` moves it into the registry, so
# it must resolve the same way ``skills`` does — a session-anchored path would
# put the agent's draft writes somewhere ``skill_publish``/``skill_discard``
# (which read/write skill-drafts/<name>/ at the workspace root) can't find them.
MANAGED_PREFIXES: frozenset[str] = frozenset({
    "memory", "ingested", "skills", "skill-drafts", "sessions", "souls",
    "workflows", "workflows-runs", "cron", "work",
})


def session_work_dir(workspace: Path, session_key: str) -> Path:
    """Return the per-session work directory under the workspace."""
    return workspace / "work" / SessionManager.safe_key(session_key)


def anchored_base(rel_first_segment: str, workspace: Path, work_dir: Path | None) -> Path:
    """Return the base a relative path resolves against.

    Managed prefixes (and any path when there is no work dir) anchor to the
    workspace root; everything else anchors to the session work dir.
    """
    if work_dir is None or rel_first_segment in MANAGED_PREFIXES:
        return workspace
    return work_dir


def read_base(rel: Path, workspace: Path, work_dir: Path | None) -> Path:
    """The base the relative path *rel* resolves against for a read: as
    ``anchored_base``, except that a path the work dir does not have, into a
    folder at the workspace root (a repository or data folder kept there), is
    read from the root. A file at the root itself never is — a stray file left
    there by another run must not stand in for one missing here — and a write
    never falls back: it lands in the work dir."""
    first = rel.parts[0] if rel.parts else ""
    base = anchored_base(first, workspace, work_dir)
    if (base != workspace and first not in ("", ".", "..")
            and not (base / rel).exists() and (workspace / first).is_dir()):
        return workspace
    return base


def display_path(path: Path, workspace: Path | None, work_dir: Path | None) -> str:
    """The way to print *path* so that resolving it by the rule above leads
    back to it: "." for the folder relative paths resolve in, relative to the
    work dir when it lies there, relative to the workspace when it lies there
    and would anchor to the workspace root, and absolute otherwise. All three
    paths are compared as given, so pass them resolved alike."""
    if path == (work_dir if work_dir is not None else workspace):
        return "."
    if work_dir is not None:
        try:
            rel = path.relative_to(work_dir)
        except ValueError:
            pass
        else:
            if rel.parts[0] not in MANAGED_PREFIXES:
                return rel.as_posix()
    if workspace is not None:
        try:
            rel = path.relative_to(workspace)
        except ValueError:
            pass
        else:
            if rel.parts and anchored_base(rel.parts[0], workspace, work_dir) == workspace:
                return rel.as_posix()
    return str(path)
