"""Working folders for workflow file hand-off, keyed by (run, node, iteration). The engine
gives every sequential node of a run ONE shared folder (node ``"work"``, no iteration) so
their created/edited files accumulate in one place and each stage sees the prior work;
parallel branch forks use per-(branch, iteration) folders so concurrent writers can't
collide before reconciliation. The tree gitignores itself and is pruned to recent runs.

``keyed_work_dir`` is the other way a run gets a working folder: a caller-supplied
``work_key`` names a STABLE folder under ``keys/`` that persists across separate run_ids —
the production entrance for the reuse gate (see ``WorkflowEngine.run``'s ``work_key``
param), since a fresh per-run folder always starts with an empty provenance ledger."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

ARTIFACT_ROOT = ".workflow"

# Subtree of ARTIFACT_ROOT holding keyed (not per-run) working folders. Named once so
# prune_runs can exclude it by identity rather than by re-typing the literal.
KEYS_DIRNAME = "keys"

_SAFE_KEY_MAX_CHARS = 80
_UNSAFE_KEY_CHARS = re.compile(r"[^a-z0-9._-]")


def safe_key(value: str) -> str:
    """Sanitize an arbitrary caller-supplied string (a workflow name or a work_key)
    into a single filesystem-safe path segment: lowercased, every character outside
    ``[a-z0-9._-]`` replaced with ``_`` (so ``"Ticket #23124"`` -> ``"ticket__23124"``,
    one ``_`` per replaced character — not collapsed), leading dots stripped (no
    hidden-file-style names), and capped at 80 chars.

    A path separator (``/`` or ``\\``) in the input is rejected outright (``ValueError``)
    rather than merely neutralized: it is the one signal that the caller handed a PATH,
    not an opaque label, and this function's whole job is to guarantee its result is
    exactly one path segment — fail loud instead of silently defusing it into a
    same-level sibling name. Also raised when the result sanitizes to nothing at all,
    or to dots only (both are unusable as a directory name)."""
    if "/" in value or "\\" in value:
        raise ValueError(f"key {value!r} must not contain a path separator")
    cleaned = _UNSAFE_KEY_CHARS.sub("_", value.lower()).lstrip(".")[:_SAFE_KEY_MAX_CHARS]
    if not cleaned or not cleaned.strip("."):
        raise ValueError(f"key {value!r} sanitizes to an empty or invalid path segment")
    return cleaned


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


def keyed_work_dir(base: str | Path, workflow_name: str, work_key: str) -> Path:
    """The STABLE working folder for a (workflow, work_key) pair:
    ``.workflow/keys/<safe(workflow_name)>/<safe(work_key)>/work``. Unlike
    ``artifact_dir``'s per-run_id folders, this same path is returned for every run
    sharing this workflow + key — the production entrance for the reuse gate: an
    engine-written ``.provenance.json`` here survives across separate run_ids, so a
    later run's reuse check finds what an earlier one stamped instead of reading an
    always-empty ledger. Raises ``ValueError`` (via ``safe_key``) on an unusable name."""
    d = Path(base) / ARTIFACT_ROOT / KEYS_DIRNAME / safe_key(workflow_name) / safe_key(work_key) / "work"
    d.mkdir(parents=True, exist_ok=True)
    return d


def prune_runs(base: str | Path, keep: int = 20, protect: set[str] | None = None) -> None:
    """Best-effort: keep the `keep` most-recent run subtrees, remove older ones.

    ``protect`` names run ids that are never deleted and never counted toward
    ``keep`` — the caller passes the runs still executing or paused awaiting
    resume. Age alone cannot protect a live run: a long node freezes its
    folder's mtime, so enough newer runs starting during it would push the
    live run out of the retained window and delete its files mid-run.

    ``KEYS_DIRNAME`` (``keys/``) is never a candidate: it holds keyed working
    folders, not per-run ones — it has no run_id, so age-based pruning would
    eventually reap it once enough ordinary runs accumulate. Retention there is
    manual (the caller/operator's responsibility), not this sweep's.
    """
    try:
        root = Path(base) / ARTIFACT_ROOT
        if not root.is_dir():
            return
        protected = protect or set()
        runs = sorted((p for p in root.iterdir()
                      if p.is_dir() and p.name != KEYS_DIRNAME and p.name not in protected),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for old in runs[keep:]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass
