"""Per-node output folders for workflow file hand-off, keyed by (run, node, iteration)
so they never collide across loop-back re-iterations or concurrent/repeated runs. The
tree gitignores itself and is pruned to a bounded number of recent runs."""
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


def artifact_dir(base: str | Path, run_id: str, node_id: str, iteration: int) -> Path:
    d = _root(base) / run_id / node_id / str(iteration)
    d.mkdir(parents=True, exist_ok=True)
    return d


def prune_runs(base: str | Path, keep: int = 20) -> None:
    """Best-effort: keep the `keep` most-recent run subtrees, remove older ones."""
    try:
        root = Path(base) / ARTIFACT_ROOT
        if not root.is_dir():
            return
        runs = sorted((p for p in root.iterdir() if p.is_dir()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for old in runs[keep:]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass
