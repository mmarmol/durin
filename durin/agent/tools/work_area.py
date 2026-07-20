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

__all__ = ["MANAGED_PREFIXES", "session_work_dir", "anchored_base"]

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
