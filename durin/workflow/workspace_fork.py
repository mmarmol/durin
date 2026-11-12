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

Writing the reconciled result back into the real folder (``apply_all``) refuses a
working folder that is a link or lies on another volume than its run area, and a
result that would change a path's shape, meets a link, a file that is not regular,
another volume or a path that cannot be examined; otherwise it keeps an undo
journal beside the working folder (``pending-apply``) until the last file is
written. A process that dies in the middle leaves the journal, and whoever uses the
folder next undoes the write-back (``restore_pending``), so a step entered again
starts from what its first attempt started from. The undo acts on a path only while
it holds what the write-back left there: a path someone else changed in between is
left as it is and named in a warning.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from loguru import logger

from durin.utils.atomic_write import atomic_write_text

# Heavy or machine-managed directories that should never be copied into a branch fork
# nor diffed: copying them per branch would be ruinous and they are not branch output.
_EXCLUDE = {".git", "node_modules", ".venv", "venv", "__pycache__", ".durin", ".mypy_cache", ".workflow"}

_AREA = ".workflow"
_BRANCHES = "branches"
# What a copy's parent name may look like: a run id is a plain name, never a path.
_PLAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# The undo journal of a write-back: `pending-apply/journal.json`, the earlier
# content of entry n in `pending-apply/pre/<n>`, and `pending-apply/tmp` where each
# new file is staged before it is renamed into the working folder.
_PENDING = "pending-apply"
_JOURNAL = "journal.json"
_PRE = "pre"
_TMP = "tmp"
_STORED_NAME = re.compile(r"[0-9]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")


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
    genuinely divergent writes (different content, or write-vs-delete) conflict. So
    do a file one branch writes and a file another branch writes inside a folder of
    that name: the folder cannot be both."""
    variants: dict[str, set] = {}
    for cs in changesets:
        for path, content in {**cs.created, **cs.modified}.items():
            variants.setdefault(path, set()).add(_hash(content))
        for path in cs.deleted:
            variants.setdefault(path, set()).add(None)   # deletion vs a write also diverges
    found = {path for path, seen in variants.items() if len(seen) > 1}
    written = {path for cs in changesets for path in {**cs.created, **cs.modified}}
    for path in written:
        found |= {folder for folder in _ancestors(path) if folder in written}
        if any(folder in written for folder in _ancestors(path)):
            found.add(path)
    return found


class PendingApplyError(Exception):
    """A write-back journal is present and cannot be used (unreadable, or beside a
    working folder that is a link): ``path`` is the journal file, or the link
    standing where the journal folder should be."""

    def __init__(self, path: str | Path, reason: str) -> None:
        super().__init__(f"the write-back journal {str(path)!r} cannot be used: {reason}")
        self.path = Path(path)


class UndoError(Exception):
    """An undo of a write-back that could not finish: ``path`` is the journal file,
    which stays so the undo can run again; ``cause`` says which paths it could not
    put back and why."""

    def __init__(self, path: str | Path, cause: BaseException) -> None:
        super().__init__(f"undoing the write-back recorded in {str(path)!r} stopped: {cause}")
        self.path = Path(path)
        self.cause = cause


SHAPE_REASON = "would turn a file into a folder or a folder into a file"
LINK_REASON = "is reached through a link"
IS_LINK_REASON = "is a link"
NOT_REGULAR_REASON = "is not a regular file"
OTHER_VOLUME_REASON = "is on another volume"
UNEXAMINED_REASON = "cannot be examined"
FOLDER_IS_LINK_REASON = "the run's working folder is a link"
FOLDER_ON_OTHER_VOLUME_REASON = "the run's working folder is on another volume than the run's area"
_NO_FOLDER = "has no folder of the working folder to go in"


class WriteBackRefusedError(ValueError):
    """A write-back ``apply_all`` will not start (``reason``): the working folder
    itself is a link or on another volume than its run area (``path`` None), or
    ``path`` (relative to the working folder) would change shape, is or is reached
    through a link, is not a regular file, is on another volume, or cannot be
    examined. Nothing was stored or changed."""

    def __init__(self, path: str | None, reason: str) -> None:
        super().__init__(reason if path is None else f"{path!r} {reason}")
        self.path = path
        self.reason = reason


class _UnsafePathError(OSError):
    """At the moment of a filesystem operation, the folders between the working
    folder and the path are not all real folders, or the path is no longer a
    regular file: the operation is not done."""

    def __init__(self, rel: str, reason: str) -> None:
        super().__init__(f"{rel!r} {reason}")


class _UndoStoppedError(Exception):
    """The paths an undo could not put back, and why."""


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
    a relative POSIX path with no empty, ``.`` or ``..`` part."""
    if not isinstance(path, str) or not path or path.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _is_mode(mode: object) -> bool:
    return isinstance(mode, int) and not isinstance(mode, bool) and 0 <= mode <= 0o7777


def _ancestors(rel: str) -> list[str]:
    """The folders above *rel* inside the working folder, outermost first."""
    parts = rel.split("/")[:-1]
    return ["/".join(parts[:depth]) for depth in range(1, len(parts) + 1)]


def _lstat(path: Path) -> os.stat_result | None:
    """``os.lstat`` of *path*, or None when the system says there is no such file or
    directory. Any other error (a folder above it without search permission, a file
    above it) is raised: such a path is not known to be absent."""
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _chain(work: Path, rel: str) -> str:
    """What stands between the working folder and *rel* right now: ``"ok"`` when
    every folder above it is a real folder (so the path is inside the working
    folder), ``"link"`` when one is a link, ``"file"`` when one is not a folder,
    ``"missing"`` when one does not exist. A folder that cannot be examined raises
    ``OSError``."""
    for folder in _ancestors(rel):
        st = _lstat(work / folder)
        if st is None:
            return "missing"
        if stat.S_ISLNK(st.st_mode):
            return "link"
        if not stat.S_ISDIR(st.st_mode):
            return "file"
    return "ok"


def _guard(work: Path, rel: str) -> None:
    """The check before every filesystem operation on *rel*: the folders above it
    are real folders inside the working folder, or ``_UnsafePathError``."""
    chain = _chain(work, rel)
    if chain != "ok":
        raise _UnsafePathError(rel, LINK_REASON if chain == "link" else _NO_FOLDER)


def _make_folders(work: Path, rel: str) -> None:
    """Make the folders above *rel* that do not exist, outermost first, each only
    inside a real folder of the working folder."""
    for folder in _ancestors(rel):
        try:
            st = os.lstat(work / folder)
        except FileNotFoundError:
            os.mkdir(work / folder)
            continue
        if stat.S_ISLNK(st.st_mode):
            raise _UnsafePathError(rel, LINK_REASON)
        if not stat.S_ISDIR(st.st_mode):
            raise _UnsafePathError(rel, _NO_FOLDER)


def _stage_into(work: Path, rel: str, fill: Callable[[BinaryIO], object], mode: int,
                staging: Path) -> None:
    """Put a regular file at *rel*: it is filled in *staging* (inside
    ``pending-apply``), given *mode*, and renamed into place once the check holds.
    A rename that fails raises; the staged file is removed."""
    staging.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=staging, prefix=".stage.")
    try:
        with os.fdopen(fd, "wb") as out:
            fill(out)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, mode)
        _guard(work, rel)
        os.replace(tmp, work / rel)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise


def _write_file(target: Path, content: bytes, work: Path, staging: Path) -> None:
    """Write one file of a write-back into the real folder, staged and renamed into
    place (an existing file keeps its mode, a new one gets 0644): the only function
    ``apply_all`` writes a file through."""
    rel = target.relative_to(work).as_posix()
    _make_folders(work, rel)
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        st = None
    if st is not None and not stat.S_ISREG(st.st_mode):
        raise _UnsafePathError(rel, "is no longer a regular file")
    mode = stat.S_IMODE(st.st_mode) if st is not None else 0o644
    _stage_into(work, rel, lambda out: out.write(content), mode, staging)


def _store_copy(source: Path, stored: Path) -> None:
    """Copy *source*'s current bytes to *stored*, streamed, and flush them to disk:
    the journal that points at them is written after."""
    with open(source, "rb") as src, open(stored, "wb") as out:
        shutil.copyfileobj(src, out)
        out.flush()
        os.fsync(out.fileno())


def _holds(path: Path, st: os.stat_result, stored: Path, mode: int) -> bool:
    """Whether *path*, found as *st*, is a regular file with *mode* holding exactly
    the bytes kept in *stored*, compared streamed. A file that cannot be read
    raises."""
    if (not stat.S_ISREG(st.st_mode) or stat.S_IMODE(st.st_mode) != mode
            or st.st_size != os.stat(stored).st_size):
        return False
    with open(path, "rb") as now, open(stored, "rb") as before:
        while True:
            chunk = now.read(1 << 16)
            if chunk != before.read(1 << 16):
                return False
            if not chunk:
                return True


def _file_hash(path: Path) -> str:
    """The SHA-256 of *path*'s bytes, read streamed."""
    digest = hashlib.sha256()
    with open(path, "rb") as src:
        for chunk in iter(lambda: src.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _put_back(work: Path, rel: str, stored: Path, mode: int, staging: Path) -> None:
    """Give *rel* back the bytes kept in *stored* (streamed) and *mode*, staged and
    renamed into place, making the folders above it as needed."""
    _make_folders(work, rel)

    def fill(out: BinaryIO) -> None:
        with open(stored, "rb") as src:
            shutil.copyfileobj(src, out)

    _stage_into(work, rel, fill, mode, staging)


def _fsync_dir(folder: Path) -> None:
    with suppress(OSError):
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _refusal(work: Path, deleted: list[str], written: list[str]) -> list[str]:
    """Check the whole write-back against the real folder and return the deletes of
    paths that exist: a delete of a path the system says does not exist is
    dropped. Raises ``WriteBackRefusedError`` when the working folder is a link
    (anything done through it would act on its target) or on another volume than
    its run area (a file staged in ``pending-apply`` could not be renamed into it),
    and for a written path another written path needs as a folder, a folder above
    a touched path that is a link, a folder above a written path that is a file, a
    target that is a link, a folder or not a regular file, a path whose nearest
    existing folder is on another volume than the working folder, or a path that
    cannot be examined (any error but "no such file or directory", such as a
    folder above it without search permission)."""
    st = os.lstat(work)
    if stat.S_ISLNK(st.st_mode):
        raise WriteBackRefusedError(None, FOLDER_IS_LINK_REASON)
    device = st.st_dev
    if os.lstat(run_area(work)).st_dev != device:
        raise WriteBackRefusedError(None, FOLDER_ON_OTHER_VOLUME_REASON)
    needed = {folder for rel in written for folder in _ancestors(rel)}
    for rel in written:
        if rel in needed:
            raise WriteBackRefusedError(rel, SHAPE_REASON)
    deletes: list[str] = []
    writing = set(written)
    for rel in dict.fromkeys([*deleted, *written]):
        try:
            # A delete asks the system first whether its path exists; a write
            # looks at the folders above its path first, which may not exist yet.
            target = None if rel in writing else _lstat(work / rel)
            if rel not in writing and target is None:
                continue
            chain = _chain(work, rel)
            if chain == "link":
                raise WriteBackRefusedError(rel, LINK_REASON)
            if chain == "file":
                raise WriteBackRefusedError(rel, SHAPE_REASON)
            if rel in writing:
                target = _lstat(work / rel)
            if target is not None:
                if stat.S_ISLNK(target.st_mode):
                    raise WriteBackRefusedError(rel, IS_LINK_REASON)
                if stat.S_ISDIR(target.st_mode):
                    raise WriteBackRefusedError(rel, SHAPE_REASON)
                if not stat.S_ISREG(target.st_mode):
                    raise WriteBackRefusedError(rel, NOT_REGULAR_REASON)
            nearest = next((work / f for f in reversed(_ancestors(rel))
                            if _lstat(work / f) is not None), work)
            if os.lstat(nearest).st_dev != device:
                raise WriteBackRefusedError(rel, OTHER_VOLUME_REASON)
        except OSError as exc:
            raise WriteBackRefusedError(rel, f"{UNEXAMINED_REASON}: {exc}") from exc
        if rel not in writing:
            deletes.append(rel)
    return deletes


def apply_all(changesets: list[ChangeSet], root: str | Path, *, run_id: str,
              node_id: str) -> None:
    """Write the changes of *changesets* into the real working folder *root*: the
    only way a ``choose`` winner or a ``union`` is written there.

    The whole write-back is first checked against the real folder and refused
    (``WriteBackRefusedError``, nothing stored or changed) when the working folder
    is a link or on another volume than its run area, or a path would change shape,
    is a link or is reached through one, is not a regular file, is on another
    volume, or cannot be examined (see ``_refusal``). A delete of a path the system
    says does not exist is dropped. Then every path it touches is recorded in
    ``pending_dir(root)``: an existing file's bytes go to ``pre/<n>`` (streamed) with
    its mode, a path that did not exist is noted as such, a written path keeps the
    SHA-256 of the content written there, and every folder the write-back creates is
    noted. The run area's entry for ``pending-apply`` is flushed to disk, and
    ``journal.json`` is written last: the journal file existing is what says a
    write-back is pending.

    Then every delete runs, then every write, each changeset's in turn; each file is
    staged in ``pending-apply/tmp`` and renamed into place. Before every operation
    the folders above the path must be real folders of the working folder, or the
    write-back fails. The journal and its folder go last. A process that dies in
    between leaves the journal for ``restore_pending``.

    Raises ``PendingApplyError`` when a journal is already present, and
    ``ValueError`` for a folder ``pending_dir`` refuses, a path that breaks the path
    rule, or a path the changesets both delete and write, or write with two
    different contents (a union conflict, which never reaches here): the undo
    recognises a written path by the one content written there. Any other
    exception leaves the journal for ``restore_pending``."""
    work = Path(root)
    pending = pending_dir(work)
    journal = pending / _JOURNAL
    if journal.exists() or journal.is_symlink():
        raise PendingApplyError(journal, "a write-back is already pending in this folder")
    writes = [item for cs in changesets for item in {**cs.created, **cs.modified}.items()]
    deleted = [rel for cs in changesets for rel in cs.deleted]
    for rel in dict.fromkeys([*deleted, *(r for r, _ in writes)]):
        if not _plain_relative(rel):
            raise ValueError(f"{rel!r} is not a plain relative path inside the folder")
    content: dict[str, str] = {}
    for rel, data in writes:
        if content.setdefault(rel, _hash(data)) != _hash(data):
            raise ValueError(f"{rel!r} is written with two different contents")
    for rel in deleted:
        if rel in content:
            raise ValueError(f"{rel!r} is both deleted and written")
    written = list(content)
    deletes = _refusal(work, deleted, written)

    shutil.rmtree(pending, ignore_errors=True)     # a leftover with no journal
    pre = pending / _PRE
    pre.mkdir(parents=True)
    _fsync_dir(pending.parent)                     # the run area now names pending-apply
    entries: list[dict] = []
    for n, rel in enumerate([*deletes, *written]):
        target = work / rel
        st = _lstat(target)
        entry = {"path": rel, "pre": None, "mode": None, "written": content.get(rel)}
        if st is not None and stat.S_ISREG(st.st_mode):
            _store_copy(target, pre / str(n))
            entry.update(pre=str(n), mode=stat.S_IMODE(st.st_mode))
        entries.append(entry)
    new_dirs = list(dict.fromkeys(
        folder for rel in written for folder in _ancestors(rel)
        if _lstat(work / folder) is None))
    _fsync_dir(pre)
    atomic_write_text(journal, json.dumps({"run_id": run_id, "node_id": node_id,
                                           "entries": entries, "new_dirs": new_dirs}),
                      fsync_dir=True)

    staging = pending / _TMP
    for rel in deletes:
        _guard(work, rel)
        (work / rel).unlink(missing_ok=True)
    for rel, data in writes:
        _write_file(work / rel, data, work, staging)
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
    for folder in (pre, pending / _TMP):
        if folder.is_symlink():
            raise PendingApplyError(journal, f"{str(folder)!r} is a symlink")
    for entry in entries:
        if not isinstance(entry, dict) or not _plain_relative(entry.get("path")):
            raise PendingApplyError(journal, f"entry {entry!r} names no path inside the folder")
        stored, mode, written = entry.get("pre"), entry.get("mode"), entry.get("written")
        if written is not None and not (isinstance(written, str) and _SHA256.fullmatch(written)):
            raise PendingApplyError(journal, f"entry {entry!r} has no usable content hash")
        if stored is None:
            if written is None:
                raise PendingApplyError(journal, f"entry {entry!r} names neither a stored "
                                                 "copy nor the content written there")
            continue
        if not isinstance(stored, str) or not _STORED_NAME.fullmatch(stored):
            raise PendingApplyError(journal, f"entry {entry!r} names no stored copy")
        if not _is_mode(mode):
            raise PendingApplyError(journal, f"entry {entry!r} has no file mode")
        copy = pre / stored
        if copy.is_symlink() or not copy.is_file():
            raise PendingApplyError(journal, f"the stored copy {str(copy)!r} is missing")
    for folder in new_dirs:
        if not _plain_relative(folder):
            raise PendingApplyError(journal, f"new directory {folder!r} is not inside the folder")
    return data


def _found(work: Path, pending: Path, entry: dict) -> str:
    """What *entry*'s path holds now, examined without following a link at it:
    ``"link"`` when a folder above it is a link; ``"earlier"`` when it holds what
    it held before the write-back (nothing, for a path that did not exist; the
    stored bytes and mode otherwise); ``"written"`` when it is a regular file
    holding the content the write-back wrote there; ``"absent"`` when the system
    says it does not exist; ``"other"`` for anything else, which someone else put
    there. Raises ``OSError`` when the path cannot be examined."""
    rel, stored = entry["path"], entry.get("pre")
    chain = _chain(work, rel)
    if chain == "link":
        return "link"
    if chain == "file":                      # a file stands where a folder above was
        return "earlier" if stored is None else "other"
    st = _lstat(work / rel) if chain == "ok" else None
    if st is None:
        return "earlier" if stored is None else "absent"
    if stored is not None and _holds(work / rel, st, pending / _PRE / stored, entry["mode"]):
        return "earlier"
    if (entry.get("written") is not None and stat.S_ISREG(st.st_mode)
            and _file_hash(work / rel) == entry["written"]):
        return "written"
    return "other"


def _undo(work: Path, pending: Path, data: dict) -> list[str]:
    """Put the folder back from a checked journal, acting on a path only when it
    holds what the write-back left there, and return the paths left as they are
    because someone else changed them.

    A file the write-back created is removed when it holds the content written
    there. A folder it created is removed when empty, deepest first, and left when
    it holds something else. A stored file is put back when it holds the content
    written there, or, for a path the write-back deleted, when it is absent; one
    that holds its earlier bytes and mode is not touched. Anything else at a path
    (other bytes, a link, a folder, a file where a folder above it was) was put
    there by someone else and is left. A path that cannot be examined, that is
    reached through a link (at a folder above it, or at a folder the write-back
    created), or whose operation fails, is left as it is, the other paths are still
    done, and ``_UndoStoppedError`` then names every such path."""
    staging = pending / _TMP
    stopped: list[str] = []
    left: list[str] = []

    def examine(entry: dict) -> str | None:
        try:
            found = _found(work, pending, entry)
        except OSError as exc:
            stopped.append(f"{entry['path']!r} {UNEXAMINED_REASON}: {exc}")
            return None
        if found == "link":
            stopped.append(f"{entry['path']!r} {LINK_REASON}")
            return None
        if found == "other" or (found == "absent" and entry.get("written") is not None):
            left.append(entry["path"])
            return None
        return found

    entries = data["entries"]
    for entry in entries:
        if entry.get("pre") is not None or examine(entry) != "written":
            continue
        try:
            (work / entry["path"]).unlink()
        except OSError as exc:
            stopped.append(f"{entry['path']!r}: {exc}")
    for folder in sorted(data["new_dirs"], key=lambda f: f.count("/"), reverse=True):
        path = work / folder
        try:
            chain = _chain(work, folder)
            st = _lstat(path) if chain == "ok" else None
        except OSError as exc:
            stopped.append(f"{folder!r} {UNEXAMINED_REASON}: {exc}")
            continue
        if chain == "link" or (st is not None and stat.S_ISLNK(st.st_mode)):
            stopped.append(f"{folder!r} {LINK_REASON}")
            continue
        if st is None or not stat.S_ISDIR(st.st_mode):
            continue
        try:
            path.rmdir()
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                stopped.append(f"{folder!r}: {exc}")
    for entry in entries:
        if entry.get("pre") is None or examine(entry) not in ("written", "absent"):
            continue
        rel = entry["path"]
        try:
            _put_back(work, rel, pending / _PRE / entry["pre"], entry["mode"], staging)
        except _UnsafePathError as exc:
            stopped.append(str(exc))
        except OSError as exc:
            stopped.append(f"{rel!r}: {exc}")
    if stopped:
        raise _UndoStoppedError("; ".join(stopped))
    return left


def restore_pending(work_dir: str | Path) -> bool:
    """Undo a write-back whose journal is still present (``apply_all``) and return
    True; return False when there is no journal (a leftover journal folder without
    one is removed), whatever the working folder is.

    Nothing is done, and ``PendingApplyError`` is raised, when the journal cannot be
    used (unreadable, a path outside the folder, an entry with neither a stored copy
    nor a content hash, a stored copy missing, or a symlink at the journal, at
    ``pending-apply`` or at its ``pre`` or ``tmp`` folder), or when the working
    folder it belongs to is a link (the undo would act on the link's target). The
    undo itself is ``_undo``; the paths it leaves because someone else changed them
    are named in one warning. When it cannot do a path, it raises ``UndoError``
    naming the paths and keeps the journal, so it runs again once the cause is gone.
    The journal goes before its stored copies. Raises ``ValueError`` for a folder
    ``run_area`` refuses."""
    work = Path(work_dir)
    link = run_area(work) / _PENDING
    if link.is_symlink():
        raise PendingApplyError(link, "the journal folder is a symlink")
    pending = pending_dir(work)
    data = _read_journal(pending)
    if data is None:
        shutil.rmtree(pending, ignore_errors=True)
        return False
    journal = pending / _JOURNAL
    if work.is_symlink():
        raise PendingApplyError(journal, "the working folder is a link")
    try:
        left = _undo(work, pending, data)
        journal.unlink()
    except Exception as exc:
        raise UndoError(journal, exc) from exc
    if left:
        logger.warning("undoing the write-back recorded in {!r} left {!r} as they are: "
                       "someone else changed them after the write-back",
                       str(journal), left)
    shutil.rmtree(pending, ignore_errors=True)
    return True


def cleanup(fork_dir: str | Path) -> None:
    """Best-effort removal of a branch fork directory."""
    shutil.rmtree(fork_dir, ignore_errors=True)

