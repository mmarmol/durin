"""Working folders for workflow file hand-off, keyed by (run, node, iteration). The engine
gives every sequential node of a run ONE shared folder (node ``"work"``, no iteration) so
their created/edited files accumulate in one place and each stage sees the prior work;
parallel branch forks use per-(branch, iteration) folders so concurrent writers can't
collide before reconciliation. The tree gitignores itself and is pruned to recent runs."""
from __future__ import annotations

import shutil
from pathlib import Path

ARTIFACT_ROOT = ".workflow"


def _root(base: str | Path) -> Path:
    root = Path(base) / ARTIFACT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    gi = root / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n")          # the whole artifact tree ignores itself
    return root


def artifact_dir(base: str | Path, run_id: str, node_id: str, iteration: int | None) -> Path:
    # ``iteration=None`` yields ONE stable folder for the node (a self-looping node
    # accumulates its files there across iterations); an int keeps the per-iteration
    # folders used by linear/fan-out hand-off so re-iterations don't collide.
    d = _root(base) / run_id / node_id
    if iteration is not None:
        d = d / str(iteration)
    d.mkdir(parents=True, exist_ok=True)
    return d


def prune_runs(base: str | Path, keep: int = 20, protect: set[str] | None = None) -> None:
    """Best-effort: keep the `keep` most-recent run subtrees, remove older ones.

    ``protect`` names run ids that are never deleted and never counted toward
    ``keep`` — the caller passes the runs still executing or paused awaiting
    resume. Age alone cannot protect a live run: a long node freezes its
    folder's mtime, so enough newer runs starting during it would push the
    live run out of the retained window and delete its files mid-run.
    """
    try:
        root = Path(base) / ARTIFACT_ROOT
        if not root.is_dir():
            return
        protected = protect or set()
        runs = sorted((p for p in root.iterdir() if p.is_dir() and p.name not in protected),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for old in runs[keep:]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass
