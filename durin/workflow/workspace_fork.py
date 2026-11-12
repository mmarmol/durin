"""Per-branch folder isolation + reconciliation for writing-in-parallel.

A writing parallel branch runs against a private COPY of the run's shared working
folder so concurrent branches don't clobber each other's file writes. After the
branches finish, their changes are reconciled back into the real folder:

- ``choose``: keep one branch's changes (a judge picks the winner), discard the rest.
- ``union``: apply every branch's changes; a same-file conflict (two branches touched
  the same path) is detected and reported rather than silently merged.

Copy-based (no git requirement, works whether or not the workspace is a repo) and pure
(no LLM, no threads), so the reconcile logic is fully unit-testable on its own.

A branch's copy lives in the run's own area, beside its working folder:
``<run area>/branches/<run id>/<label>-XXXX``. The run area is the folder that
holds the working folder (``<workspace>/.workflow/<run id>`` for an unkeyed run,
the key's folder for a keyed one), so whatever removes the run's area removes its
copies, and a process that dies mid-step leaves them where the next entry of the
step, or the conversion of the dead run, finds and removes them. Nothing here
creates or removes anything outside a run area: a folder that does not lie under
``.workflow`` is refused.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Heavy or machine-managed directories that should never be copied into a branch fork
# nor diffed: copying them per branch would be ruinous and they are not branch output.
_EXCLUDE = {".git", "node_modules", ".venv", "venv", "__pycache__", ".durin", ".mypy_cache", ".workflow"}

_AREA = ".workflow"
_BRANCHES = "branches"
# What a copy's parent name may look like: a run id is a plain name, never a path.
_PLAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass
class ChangeSet:
    """The file changes one branch made, relative to the base workspace snapshot."""

    created: dict[str, bytes]
    modified: dict[str, bytes]
    deleted: set[str]

    @property
    def paths(self) -> set[str]:
        return set(self.created) | set(self.modified) | set(self.deleted)


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _walk(root: Path) -> dict[str, Path]:
    """Relative-path -> absolute-path for every file under *root*, excluding heavy
    dirs."""
    out: dict[str, Path] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in _EXCLUDE for part in rel.parts):
            continue
        out[str(rel)] = p
    return out


def snapshot(root: str | Path) -> dict[str, str]:
    """Capture the base state as relative-path -> content hash."""
    return {rel: _hash(p.read_bytes()) for rel, p in _walk(Path(root)).items()}


def run_area(work_dir: str | Path) -> Path:
    """The folder that holds a run's working folder and everything else the run
    owns. Raises ``ValueError`` unless the working folder lies under a
    ``.workflow`` directory and sits at least one level inside a run's own folder
    (the ``.workflow`` directory itself, or a folder directly inside it, is not a
    run's)."""
    work = Path(os.path.abspath(work_dir))
    area = work.parent
    if _AREA not in work.parts or work.name == _AREA or area.name == _AREA:
        raise ValueError(f"{str(work_dir)!r} is not a run's working folder under {_AREA}/")
    return area


def branches_root(work_dir: str | Path) -> Path:
    """Where the private copies of a run's parallel steps live: ``branches`` beside
    the working folder. Raises ``ValueError`` for a folder ``run_area`` refuses."""
    return run_area(work_dir) / _BRANCHES


def _safe_label(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", label).lstrip(".-") or "b"


def fork(root: str | Path, *, into: str | Path, label: str = "") -> Path:
    """Copy the working folder *root* (excluding heavy dirs) into a fresh directory
    made inside *into*, so a branch can write there in isolation. *into* must lie
    under ``branches_root(root)``, otherwise ``ValueError`` and nothing is created.
    The caller owns removal (``clear_branches``, or ``cleanup`` for one copy)."""
    src = Path(root)
    base = branches_root(src)
    target_parent = Path(os.path.abspath(into))
    if target_parent != base and base not in target_parent.parents:
        raise ValueError(f"{str(into)!r} is not under {str(base)!r}")
    target_parent.mkdir(parents=True, exist_ok=True)
    safe = _safe_label(label)
    dst = Path(tempfile.mkdtemp(prefix=f"{safe}-" if label else "b-", dir=target_parent))
    for rel, p in _walk(src).items():
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    return dst


def clear_branches(work_dir: str | Path, run_id: str | None = None) -> None:
    """Remove one run's copies (``branches/<run_id>``), or every run's and every
    retired leftover (the whole ``branches`` folder) when *run_id* is None.
    Best-effort and never raises; a folder ``run_area`` refuses is left alone."""
    try:
        root = branches_root(work_dir)
        if run_id is None:
            shutil.rmtree(root, ignore_errors=True)
        elif _PLAIN_NAME.fullmatch(run_id):
            shutil.rmtree(root / run_id, ignore_errors=True)
    except Exception:  # noqa: BLE001 - a clean-up never stops the caller
        pass


def retire_branches(work_dir: str | Path, run_id: str) -> Path | None:
    """Rename one run's copies to a name nothing else uses
    (``branches/.dead-<run_id>-XXXX``) and return it, or None when there is
    nothing to rename or the folder is refused. A rename is fast, so it can be done
    under a lock; the caller removes the returned directory afterwards (``cleanup``)
    without racing a new attempt that makes copies under ``branches/<run_id>``.
    Never raises."""
    try:
        root = branches_root(work_dir)
        if not _PLAIN_NAME.fullmatch(run_id) or not (root / run_id).is_dir():
            return None
        retired = Path(tempfile.mkdtemp(prefix=f".dead-{run_id}-", dir=root))
        retired.rmdir()                       # mkdtemp only reserves the unique name
        (root / run_id).rename(retired)
        return retired
    except Exception:  # noqa: BLE001 - a clean-up never stops the caller
        return None


def diff(base: dict[str, str], fork_dir: str | Path) -> ChangeSet:
    """Compute what changed in *fork_dir* relative to the *base* snapshot."""
    now = _walk(Path(fork_dir))
    created: dict[str, bytes] = {}
    modified: dict[str, bytes] = {}
    for rel, p in now.items():
        content = p.read_bytes()
        if rel not in base:
            created[rel] = content
        elif base[rel] != _hash(content):
            modified[rel] = content
    deleted = set(base) - set(now)
    return ChangeSet(created=created, modified=modified, deleted=deleted)


def conflicts(changesets: list[ChangeSet]) -> set[str]:
    """The union-mode collision set: paths touched by more than one branch with
    DIFFERING outcomes. Two branches writing identical content to the same path is
    not a conflict — agents routinely emit the same incidental file (an empty
    ``__init__.py``, a boilerplate header), and that should reconcile cleanly. Only
    genuinely divergent writes (different content, or write-vs-delete) conflict."""
    variants: dict[str, set] = {}
    for cs in changesets:
        for path, content in {**cs.created, **cs.modified}.items():
            variants.setdefault(path, set()).add(_hash(content))
        for path in cs.deleted:
            variants.setdefault(path, set()).add(None)   # deletion vs a write also diverges
    return {path for path, seen in variants.items() if len(seen) > 1}


def apply(cs: ChangeSet, root: str | Path) -> None:
    """Apply a branch's changes back to the real folder the fork copied."""
    ws = Path(root)
    for rel, content in {**cs.created, **cs.modified}.items():
        target = ws / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    for rel in cs.deleted:
        (ws / rel).unlink(missing_ok=True)


def cleanup(fork_dir: str | Path) -> None:
    """Best-effort removal of a branch fork directory."""
    shutil.rmtree(fork_dir, ignore_errors=True)
