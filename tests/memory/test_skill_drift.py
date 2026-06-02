"""Drift detect + repair must recognize indexed skills (Task 1.3).

Regression guard for the highest-risk skills-memory-class bug: drift
detection built ``fs_files`` from ``walk_memory`` only, so every indexed
skill (``uri='skill/<slug>'``) looked like a ``row_for_missing_file`` and
the auto-repair would SILENTLY DELETE it. And even a genuinely stale skill
(``mtime_lag`` / ``missing_row``) could never be repaired because
``_repair_drift`` had no ``skill/`` branch to reconstruct the path.

These tests pin both halves:
- an indexed skill backed by a real file is NOT flagged missing, and
- a drifted skill is re-indexed (not deleted) by the repair.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from durin.memory.fts_index import FTSIndex
from durin.memory.indexer import (
    detect_index_staleness,
    reindex_one_skill,
)
from durin.memory.paths import skill_uri


def _mk_skill(
    workspace: Path,
    name: str,
    *,
    desc: str = "rebase flow",
    body: str = "run git rebase interactive uniquetokenzz",
) -> Path:
    d = workspace / "skills" / name
    d.mkdir(parents=True)
    md = d / "SKILL.md"
    md.write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n",
        encoding="utf-8",
    )
    return md


def test_indexed_skill_not_flagged_missing(tmp_path: Path) -> None:
    """A skill that is indexed AND on disk must not be reported as a
    ``row_for_missing_file`` (or any drift) — otherwise repair deletes it."""
    md = _mk_skill(tmp_path, "git-helper")
    reindex_one_skill(tmp_path, md)

    issues = detect_index_staleness(tmp_path)

    uri = skill_uri("git-helper")
    flagged = {(i["uri"], i["reason"]) for i in issues}
    assert (uri, "row_for_missing_file") not in flagged
    # A clean, in-sync skill should produce no drift at all.
    assert all(i["uri"] != uri for i in issues), (
        f"indexed skill should not appear in drift issues: {issues}"
    )


def test_repair_reindexes_a_drifted_skill(tmp_path: Path) -> None:
    """A stale skill row (mtime_lag) is re-indexed by the repair, NOT
    deleted: after a tick the skill is still searchable with fresh text."""
    from durin.memory.health_check import HealthChecker

    md = _mk_skill(tmp_path, "deploy", body="kubectl apply firsttokenzz")
    reindex_one_skill(tmp_path, md)
    uri = skill_uri("deploy")

    # Edit the body and force a future mtime so detect reports mtime_lag
    # against the stale index row (without re-indexing it).
    md.write_text(
        "---\nname: deploy\ndescription: ship it\n---\n"
        "kubectl apply secondtokenzz\n",
        encoding="utf-8",
    )
    later = time.time() + 60
    os.utime(md, (later, later))

    issues = detect_index_staleness(tmp_path)
    assert any(
        i["uri"] == uri and i["reason"] == "mtime_lag" for i in issues
    ), f"expected mtime_lag for {uri}, got {issues}"

    # Run a real health tick: it detects + repairs the drift.
    HealthChecker(tmp_path).run_tick()

    # The skill row survived AND was refreshed with the new body.
    with FTSIndex.open(tmp_path) as idx:
        hits = idx.search("secondtokenzz")
    assert any(h.uri == uri and h.type == "skill" for h in hits), (
        "drifted skill should be re-indexed (not deleted) after repair"
    )
    # Drift is now clear for the skill.
    assert all(i["uri"] != uri for i in detect_index_staleness(tmp_path))
