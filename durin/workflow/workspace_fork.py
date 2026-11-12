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
``<run area>/branches/<run id>/b-XXXX``. The run area is the folder that
holds the working folder (``<workspace>/.workflow/<run id>`` for an unkeyed run,
the key's folder for a keyed one), so whatever removes the run's area removes its
copies, and a process that dies mid-step leaves them where the next entry of the
step, or the conversion of the dead run, finds and removes them. Nothing here
creates or removes anything outside a run area: a folder that does not lie under
``.workflow`` is refused.

Writing the reconciled result back into the real folder (``apply_all``) keeps an
undo journal beside the working folder (``pending-apply``) until the last file is
written. A process that dies in the middle leaves the journal, and whoever uses the
folder next puts it back as it was before the write-back (``restore_pending``), so a
step entered again starts from what its first attempt started from.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from durin.utils.atomic_write import atomic_write_bytes, atomic_write_text

# Heavy or machine-managed directories that should never be copied into a branch fork
# nor diffed: copying them per branch would be ruinous and they are not branch output.
_EXCLUDE = {".git", "node_modules", ".venv", "venv", "__pycache__", ".durin", ".mypy_cache", ".workflow"}

_AREA = ".workflow"
_BRANCHES = "branches"
# What a copy's parent name may look like: a run id is a plain name, never a path.
_PLAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# The undo journal of a write-back: `pending-apply/journal.json`, and the earlier
# content of entry n in `pending-apply/pre/<n>`.
_PENDING = "pending-apply"
_JOURNAL = "journal.json"
_PRE = "pre"
_STORED_NAME = re.compile(r"[0-9]+")


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


def _runs_copies(work_dir: str | Path, run_id: str) -> Path:
    """``branches/<run_id>`` for a working folder, or ``ValueError`` when the folder
    is refused, the run id is not a plain name, or ``branches`` or
    ``branches/<run_id>`` is a symlink (anything done through it would act on its
    target, outside the run's area)."""
    if not _PLAIN_NAME.fullmatch(run_id):
        raise ValueError(f"{run_id!r} is not a plain run id")
    root = branches_root(work_dir)
    copies = root / run_id
    if root.is_symlink() or copies.is_symlink():
        raise ValueError(f"{str(root)!r} must not be reached through a symlink")
    return copies


def fork(root: str | Path, *, into: str | Path) -> Path:
    """Copy the working folder *root* (excluding heavy dirs) into a fresh ``b-XXXX``
    directory made inside *into*, so a branch can write there in isolation. The
    name carries no branch id: the path is quoted in shell commands, whose deny
    rules match words in it. *into* must be exactly ``branches_root(root) / <run id>``
    (a plain run id, no symlink on the way), otherwise ``ValueError`` and nothing is
    created. The caller owns removal (``clear_branches``, or ``cleanup`` for one
    copy)."""
    src = Path(root)
    target_parent = Path(os.path.abspath(into))
    if _runs_copies(src, target_parent.name) != target_parent:
        raise ValueError(f"{str(into)!r} is not {str(branches_root(src))!r}/<run id>")
    target_parent.mkdir(parents=True, exist_ok=True)
    dst = Path(tempfile.mkdtemp(prefix="b-", dir=target_parent))
    for rel, p in _walk(src).items():
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
    return dst


def clear_branches(work_dir: str | Path, run_id: str | None = None) -> None:
    """Remove one run's copies (``branches/<run_id>``), or every run's and every
    retired leftover (the whole ``branches`` folder) when *run_id* is None.
    Best-effort and never raises; a folder ``run_area`` refuses, a run id that is
    not a plain name and a symlink at ``branches`` or ``branches/<run_id>`` are
    left alone."""
    try:
        if run_id is None:
            root = branches_root(work_dir)
            if not root.is_symlink():
                shutil.rmtree(root, ignore_errors=True)
        else:
            shutil.rmtree(_runs_copies(work_dir, run_id), ignore_errors=True)
    except Exception:  # noqa: BLE001 - a clean-up never stops the caller
        pass


def retire_branches(work_dir: str | Path, run_id: str) -> Path | None:
    """Rename one run's copies to a name nothing else uses
    (``branches/.dead-<run_id>-XXXX``) and return it, or None when there is
    nothing to rename or the folder is refused (see ``clear_branches``). A rename
    is fast, so it can be done under a lock; the caller removes the returned
    directory afterwards (``cleanup``) without racing a new attempt that makes
    copies under ``branches/<run_id>``. Never raises."""
    try:
        copies = _runs_copies(work_dir, run_id)
        if not copies.is_dir():
            return None
        retired = Path(tempfile.mkdtemp(prefix=f".dead-{run_id}-", dir=copies.parent))
        retired.rmdir()                       # mkdtemp only reserves the unique name
        copies.rename(retired)
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


class PendingApplyError(Exception):
    """A write-back journal is present and cannot be used: ``path`` is the
    journal file (or the link standing where the journal folder should be)."""

    def __init__(self, path: str | Path, reason: str) -> None:
        super().__init__(f"the write-back journal {str(path)!r} cannot be used: {reason}")
        self.path = Path(path)


def pending_dir(work_dir: str | Path) -> Path:
    """Where the undo journal of a write-back into *work_dir* lives:
    ``pending-apply`` beside the working folder. It belongs to the folder, not to
    a run, so a sub-workflow writing into its caller's folder and a later run on
    the same key find it in the same place. Raises ``ValueError`` for a folder
    ``run_area`` refuses, or when ``pending-apply`` is a symlink (anything done
    through it would act on its target, outside the run's area)."""
    pending = run_area(work_dir) / _PENDING
    if pending.is_symlink():
        raise ValueError(f"{str(pending)!r} must not be reached through a symlink")
    return pending


def _plain_relative(path: object) -> bool:
    """The one path rule of the journal, used when journaling and when restoring:
    a relative POSIX path with no empty, ``.`` or ``..`` part. A rule about the
    text, not about where the path resolves: a symlinked file inside the folder
    is written, stored and restored through its link."""
    if not isinstance(path, str) or not path or path.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _write_file(target: Path, content: bytes) -> None:
    """Write one file of a write-back into the real folder, replacing it in one
    step (a symlinked file is written through its link, an existing file keeps
    its mode). The only function ``apply_all`` writes a real file through."""
    atomic_write_bytes(target, content)


def _store_copy(source: Path, stored: Path) -> None:
    """Copy *source*'s current bytes to *stored*, streamed, and flush them to disk:
    the journal that points at them is written after."""
    with open(source, "rb") as src, open(stored, "wb") as out:
        shutil.copyfileobj(src, out)
        out.flush()
        os.fsync(out.fileno())


def _put_back(target: Path, stored: Path, mode: int) -> None:
    """Replace *target* in one step with the bytes kept in *stored* (streamed, never
    read whole) and give it *mode*. A symlinked target is written through its link,
    as the write-back wrote it."""
    real = Path(os.path.realpath(target)) if target.is_symlink() else target
    real.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=real.parent, prefix=f".{real.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as out, open(stored, "rb") as src:
            shutil.copyfileobj(src, out)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def _fsync_dir(folder: Path) -> None:
    with suppress(OSError):
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def apply_all(changesets: list[ChangeSet], root: str | Path, *, run_id: str,
              node_id: str) -> None:
    """Write the changes of *changesets*, in order (each one's writes, then its
    deletes), into the real working folder *root*: the only way a ``choose``
    winner or a ``union`` is written there.

    The write-back can be undone. Before anything in the folder changes, the
    earlier content of every path it touches is copied to ``pre/<n>`` in
    ``pending_dir(root)`` (a path that did not exist is noted as such, every
    directory it will create too), and ``journal.json`` is written last: the
    journal file existing is what says a write-back is pending. Then the files are
    written and deleted, and the journal and its folder removed. A process that dies
    in between leaves the journal, and ``restore_pending`` puts the folder back as
    it was before the write-back started.

    Raises ``PendingApplyError`` when a journal is already present (it is undone
    first, by ``restore_pending``), and ``ValueError`` for a folder ``pending_dir``
    refuses or a changeset path that breaks the path rule or names a directory,
    checked before anything is stored. Any other exception leaves the journal for
    ``restore_pending``."""
    work = Path(root)
    pending = pending_dir(work)
    journal = pending / _JOURNAL
    if journal.exists() or journal.is_symlink():
        raise PendingApplyError(journal, "a write-back is already pending in this folder")
    steps: list[tuple[str, bytes | None]] = []
    for cs in changesets:
        steps.extend({**cs.created, **cs.modified}.items())
        steps.extend((rel, None) for rel in cs.deleted)
    paths = list(dict.fromkeys(rel for rel, _ in steps))
    for rel in paths:
        if not _plain_relative(rel):
            raise ValueError(f"{rel!r} is not a plain relative path inside the folder")
        if (work / rel).is_dir():
            raise ValueError(f"{rel!r} is a directory in the folder")

    shutil.rmtree(pending, ignore_errors=True)     # a leftover with no journal
    pre = pending / _PRE
    pre.mkdir(parents=True)
    entries: list[dict] = []
    for n, rel in enumerate(paths):
        target = work / rel
        if target.is_file():
            _store_copy(target, pre / str(n))
            entries.append({"path": rel, "pre": str(n),
                            "mode": stat.S_IMODE(target.stat().st_mode)})
        else:
            entries.append({"path": rel, "pre": None, "mode": None})
    new_dirs: list[str] = []
    for rel, content in steps:
        if content is None:
            continue
        parts = rel.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            folder = "/".join(parts[:depth])
            if folder not in new_dirs and not (work / folder).exists():
                new_dirs.append(folder)
    _fsync_dir(pre)
    atomic_write_text(journal, json.dumps({"run_id": run_id, "node_id": node_id,
                                           "entries": entries, "new_dirs": new_dirs}),
                      fsync_dir=True)

    for rel, content in steps:
        if content is None:
            (work / rel).unlink(missing_ok=True)
        else:
            _write_file(work / rel, content)
    journal.unlink()
    shutil.rmtree(pending, ignore_errors=True)


def _read_journal(pending: Path) -> dict | None:
    """The journal in *pending*, checked whole, or None when there is none. Raises
    ``PendingApplyError`` when it is present and anything in it cannot be used."""
    journal = pending / _JOURNAL
    if journal.is_symlink():
        raise PendingApplyError(journal, "it is a symlink")
    if not journal.exists():
        return None
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PendingApplyError(journal, f"it cannot be read ({exc})") from exc
    if not isinstance(data, dict):
        raise PendingApplyError(journal, "it is not a JSON object")
    entries, new_dirs = data.get("entries"), data.get("new_dirs")
    if not isinstance(entries, list) or not isinstance(new_dirs, list):
        raise PendingApplyError(journal, "it has no list of entries or of new directories")
    pre = pending / _PRE
    for entry in entries:
        if not isinstance(entry, dict) or not _plain_relative(entry.get("path")):
            raise PendingApplyError(journal, f"entry {entry!r} names no path inside the folder")
        stored, mode = entry.get("pre"), entry.get("mode")
        if stored is None:
            continue
        if not isinstance(stored, str) or not _STORED_NAME.fullmatch(stored):
            raise PendingApplyError(journal, f"entry {entry!r} names no stored copy")
        if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777:
            raise PendingApplyError(journal, f"entry {entry!r} has no file mode")
        copy = pre / stored
        if pre.is_symlink() or copy.is_symlink() or not copy.is_file():
            raise PendingApplyError(journal, f"the stored copy {str(copy)!r} is missing")
    for folder in new_dirs:
        if not _plain_relative(folder):
            raise PendingApplyError(journal, f"new directory {folder!r} is not inside the folder")
    return data


def restore_pending(work_dir: str | Path) -> bool:
    """Put the working folder back as it was before a write-back whose journal is
    still present (``apply_all``), and return True; return False when there is no
    journal (a leftover journal folder without one is removed).

    The journal is checked whole before anything is touched: when any of it
    cannot be used (unreadable, a path outside the folder, a stored copy missing,
    or a symlink at the journal or where its folder should be) ``PendingApplyError``
    is raised and nothing changes. Then each path gets its stored bytes and mode
    back (a path that did not exist is removed), each directory the write-back
    created is removed when empty, deepest first, and the journal goes before its
    stored copies. Running it twice, or again after it was cut halfway, ends in the
    same folder. Raises ``ValueError`` for a folder ``run_area`` refuses."""
    work = Path(work_dir)
    link = run_area(work) / _PENDING
    if link.is_symlink():
        raise PendingApplyError(link, "the journal folder is a symlink")
    pending = pending_dir(work)
    data = _read_journal(pending)
    if data is None:
        shutil.rmtree(pending, ignore_errors=True)
        return False
    for entry in data["entries"]:
        target = work / entry["path"]
        if entry.get("pre") is None:
            # The path itself goes, never what a link there points at: that may
            # lie outside the run's area.
            target.unlink(missing_ok=True)
        else:
            _put_back(target, pending / _PRE / entry["pre"], entry["mode"])
    for folder in sorted(data["new_dirs"], key=lambda f: f.count("/"), reverse=True):
        with suppress(OSError):
            (work / folder).rmdir()
    (pending / _JOURNAL).unlink()
    shutil.rmtree(pending, ignore_errors=True)
    return True


def cleanup(fork_dir: str | Path) -> None:
    """Best-effort removal of a branch fork directory."""
    shutil.rmtree(fork_dir, ignore_errors=True)

