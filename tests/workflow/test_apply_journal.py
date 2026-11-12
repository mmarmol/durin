"""Writing a parallel step's result into the real working folder goes through an
undo journal: a write-back that was cut is put back whole before anything uses the
folder again (pure file logic on ``tmp_path``)."""
import errno
import json
import os
import stat
from pathlib import Path

import pytest

from durin.workflow import workspace_fork
from durin.workflow.workspace_fork import ChangeSet

CS = ChangeSet(created={"new/n.txt": b"n"},
               modified={"a.txt": b"a2", "sub/b.bin": b"\x00"},
               deleted={"exe.sh"})
# A second branch's changes, applied after CS: a cut after CS's three writes and
# its delete lands on this one's write, so a cut delete is undone too.
CS2 = ChangeSet(created={"other/deep/z.txt": b"z"}, modified={}, deleted=set())


def _folder(tmp_path: Path) -> Path:
    work = tmp_path / ".workflow" / "r1" / "work"
    (work / "sub").mkdir(parents=True)
    (work / "a.txt").write_bytes(b"a1")
    (work / "sub" / "b.bin").write_bytes(bytes(range(256)))
    (work / "exe.sh").write_bytes(b"#!/bin/sh\necho hi\n")
    os.chmod(work / "exe.sh", 0o755)
    return work


@pytest.fixture
def work(tmp_path):
    return _folder(tmp_path)


def _tree(root: Path) -> dict[str, tuple]:
    """Everything under ``root``, links not followed: a file as (bytes, mode), a
    directory as ("dir", mode), a link as ("link", its target text)."""
    out: dict[str, tuple] = {}
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            p = Path(folder) / name
            st = os.lstat(p)
            rel = p.relative_to(root).as_posix()
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("link", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", stat.S_IMODE(st.st_mode))
            elif not stat.S_ISREG(st.st_mode):         # a FIFO: never opened
                out[rel] = ("special", stat.S_IFMT(st.st_mode))
            else:
                out[rel] = (p.read_bytes(), stat.S_IMODE(st.st_mode))
    return out


def _fail_after(monkeypatch, k: int) -> list[int]:
    """Make the k+1-th real-file write of an apply raise; the first k go through."""
    real = workspace_fork._write_file
    calls = [0]

    def write(target, content, *rest):
        calls[0] += 1
        if calls[0] > k:
            raise OSError("disk full")
        return real(target, content, *rest)

    monkeypatch.setattr(workspace_fork, "_write_file", write)
    return calls


def _journal(work: Path) -> Path:
    return workspace_fork.pending_dir(work) / "journal.json"


def _plant(work: Path, body) -> Path:
    journal = _journal(work)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(body if isinstance(body, str) else json.dumps(body))
    return journal


def test_the_journal_lives_beside_the_working_folder(work):
    assert workspace_fork.pending_dir(work) == work.parent / "pending-apply"


def test_a_folder_outside_a_run_area_has_no_journal(tmp_path):
    outside = tmp_path / "project" / "work"
    outside.mkdir(parents=True)
    for call in (lambda: workspace_fork.pending_dir(outside),
                 lambda: workspace_fork.restore_pending(outside),
                 lambda: workspace_fork.apply_all([CS], outside, run_id="r1", node_id="fan")):
        with pytest.raises(ValueError):
            call()
    assert not (tmp_path / "project" / "pending-apply").exists()


def test_a_whole_apply_leaves_no_journal(work):
    workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    assert (work / "a.txt").read_bytes() == b"a2" and not (work / "exe.sh").exists()
    assert (work / "new" / "n.txt").read_bytes() == b"n"
    assert (work / "sub" / "b.bin").read_bytes() == b"\x00"
    assert not workspace_fork.pending_dir(work).exists()


@pytest.mark.parametrize("k", [0, 1, 2, 3])
def test_an_apply_cut_after_k_writes_is_put_back_whole(work, monkeypatch, k):
    before = _tree(work)
    _fail_after(monkeypatch, k)
    with pytest.raises(OSError):
        workspace_fork.apply_all([CS, CS2], work, run_id="r1", node_id="fan")
    assert _journal(work).is_file()
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == before             # bytes AND modes, binary file included
    assert not (work / "new").exists()       # a directory the apply created is gone
    assert not (work / "other").exists()
    assert not workspace_fork.pending_dir(work).exists()


def test_restoring_twice_or_after_a_cut_restore_ends_the_same(work, monkeypatch):
    before = _tree(work)
    _fail_after(monkeypatch, 2)
    with pytest.raises(OSError):
        workspace_fork.apply_all([CS, CS2], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    real = workspace_fork._put_back
    calls = [0]

    def cut(*args):
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("killed")
        return real(*args)

    monkeypatch.setattr(workspace_fork, "_put_back", cut)
    with pytest.raises(workspace_fork.UndoError) as err:
        workspace_fork.restore_pending(work)
    assert err.value.path == _journal(work) and "killed" in str(err.value.cause)
    monkeypatch.undo()
    assert _journal(work).is_file()          # a cut restore keeps its journal
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == before
    assert workspace_fork.restore_pending(work) is False
    assert _tree(work) == before


def test_nothing_is_changed_before_the_journal_file_exists(work, monkeypatch):
    before = _tree(work)
    real = workspace_fork.atomic_write_text

    def refuse_journal(path, *a, **kw):
        if Path(path).name == "journal.json":
            raise OSError("disk full")
        return real(path, *a, **kw)

    monkeypatch.setattr(workspace_fork, "atomic_write_text", refuse_journal)
    with pytest.raises(OSError):
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    assert _tree(work) == before and not (work / "new").exists()
    assert workspace_fork.restore_pending(work) is False
    assert not workspace_fork.pending_dir(work).exists()


def test_an_apply_finished_but_not_yet_cleared_is_put_back(work, monkeypatch):
    before = _tree(work)
    real = Path.unlink

    def keep_journal(self, *a, **kw):
        if self.name == "journal.json":
            raise OSError("killed")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", keep_journal)
    with pytest.raises(OSError):
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    assert (work / "a.txt").read_bytes() == b"a2"   # every change was written
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == before and not (work / "new").exists()


def test_the_journal_names_the_run_the_step_and_every_path(work, monkeypatch):
    _fail_after(monkeypatch, 0)
    with pytest.raises(OSError):
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    data = json.loads(_journal(work).read_text())
    assert data["run_id"] == "r1" and data["node_id"] == "fan"
    by_path = {e["path"]: e for e in data["entries"]}
    assert set(by_path) == {"new/n.txt", "a.txt", "sub/b.bin", "exe.sh"}
    assert by_path["new/n.txt"]["pre"] is None and by_path["new/n.txt"]["mode"] is None
    assert by_path["exe.sh"]["mode"] == 0o755
    import hashlib
    # A written path keeps the SHA-256 of the content written there; a delete has none.
    assert {p: e["written"] for p, e in by_path.items()} == {
        "new/n.txt": hashlib.sha256(b"n").hexdigest(), "a.txt": hashlib.sha256(b"a2").hexdigest(),
        "sub/b.bin": hashlib.sha256(b"\x00").hexdigest(), "exe.sh": None}
    stored = workspace_fork.pending_dir(work) / "pre" / by_path["sub/b.bin"]["pre"]
    assert stored.read_bytes() == bytes(range(256))
    assert data["new_dirs"] == ["new"]


def test_earlier_content_is_stored_on_disk_file_by_file(work, monkeypatch):
    before = _tree(work)
    real = Path.read_bytes

    def no_whole_reads(self):
        if work in self.parents:
            raise AssertionError(f"read whole: {self}")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", no_whole_reads)
    _fail_after(monkeypatch, 1)
    with pytest.raises(OSError):
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == before


def test_an_unreadable_journal_touches_nothing_and_names_the_file(work):
    before = _tree(work)
    journal = _plant(work, "{")
    with pytest.raises(workspace_fork.PendingApplyError) as err:
        workspace_fork.restore_pending(work)
    assert err.value.path == journal
    assert _tree(work) == before and journal.read_text() == "{"


@pytest.mark.parametrize("body", [
    {"run_id": "r1", "node_id": "fan", "new_dirs": [],
     "entries": [{"path": "../../outside.txt", "pre": None, "mode": None}]},
    {"run_id": "r1", "node_id": "fan", "new_dirs": [],
     "entries": [{"path": "ABSOLUTE", "pre": None, "mode": None}]},
    {"run_id": "r1", "node_id": "fan", "new_dirs": ["../.."],
     "entries": [{"path": "a.txt", "pre": None, "mode": None}]},
    {"run_id": "r1", "node_id": "fan", "new_dirs": [],
     "entries": [{"path": "a.txt", "pre": "../../../outside.txt", "mode": 420}]},
    ["not", "an", "object"],
], ids=["dotdot", "absolute", "new-dir", "pre-name", "not-an-object"])
def test_a_journal_path_outside_the_folder_touches_nothing(work, tmp_path, body):
    outside = tmp_path / ".workflow" / "outside.txt"
    outside.write_text("keep")
    if isinstance(body, dict):
        for e in body["entries"]:
            if e["path"] == "ABSOLUTE":
                e["path"] = str(outside)
    before = _tree(work)
    journal = _plant(work, body)
    with pytest.raises(workspace_fork.PendingApplyError) as err:
        workspace_fork.restore_pending(work)
    assert err.value.path == journal
    assert outside.read_text() == "keep"
    assert _tree(work) == before and journal.exists()


@pytest.mark.parametrize("entry", [
    {"path": "zzz.txt", "pre": None, "mode": None},
    {"path": "zzz.txt", "pre": None, "mode": None, "written": "not-a-hash"},
    {"path": "zzz.txt", "pre": None, "mode": None, "written": 7},
], ids=["no-hash", "bad-hash", "not-text"])
def test_a_journal_entry_without_a_usable_hash_touches_nothing(work, entry):
    (work / "zzz.txt").write_text("made by the apply")
    before = _tree(work)
    journal = _plant(work, {"run_id": "r1", "node_id": "fan", "new_dirs": [], "entries": [entry]})
    with pytest.raises(workspace_fork.PendingApplyError) as err:
        workspace_fork.restore_pending(work)
    assert err.value.path == journal
    assert _tree(work) == before


def test_a_missing_stored_file_touches_nothing(work):
    (work / "zzz.txt").write_text("made by the apply")
    before = _tree(work)
    journal = _plant(work, {"run_id": "r1", "node_id": "fan", "new_dirs": [], "entries": [
        {"path": "zzz.txt", "pre": None, "mode": None},
        {"path": "a.txt", "pre": "1", "mode": 0o644}]})
    with pytest.raises(workspace_fork.PendingApplyError) as err:
        workspace_fork.restore_pending(work)
    assert err.value.path == journal
    assert _tree(work) == before             # the first entry was not acted on either


@pytest.mark.parametrize("cs", [
    ChangeSet(created={"../x": b""}, modified={}, deleted=set()),
    ChangeSet(created={"/abs": b""}, modified={}, deleted=set()),
    ChangeSet(created={"ok.txt": b"", "a/./b": b""}, modified={}, deleted=set()),
    ChangeSet(created={"ok.txt": b""}, modified={}, deleted={"a//b"}),
    ChangeSet(created={}, modified={"a.txt": b"x"}, deleted={"../../y"}),
], ids=["dotdot", "absolute", "dot", "empty-part", "deleted-dotdot"])
def test_a_changeset_path_that_breaks_the_rule_fails_before_anything_is_stored(work, cs):
    before = _tree(work)
    with pytest.raises(ValueError):
        workspace_fork.apply_all([cs], work, run_id="r1", node_id="fan")
    assert not workspace_fork.pending_dir(work).exists()
    assert _tree(work) == before and not (work / "ok.txt").exists()


def test_an_apply_does_not_start_over_a_pending_journal(work, monkeypatch):
    _fail_after(monkeypatch, 1)
    with pytest.raises(OSError):
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    cut = _tree(work)
    journal = _journal(work)
    saved = journal.read_bytes()
    with pytest.raises(workspace_fork.PendingApplyError) as err:
        workspace_fork.apply_all([CS2], work, run_id="r2", node_id="fan")
    assert err.value.path == journal
    assert _tree(work) == cut and journal.read_bytes() == saved
    assert not (work / "other").exists()


def test_a_symlinked_journal_folder_is_refused_and_not_followed(work, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "journal.json").write_text(json.dumps(
        {"run_id": "r1", "node_id": "fan", "new_dirs": [],
         "entries": [{"path": "a.txt", "pre": None, "mode": None}]}))
    (elsewhere / "sentinel.txt").write_text("keep")
    link = workspace_fork.run_area(work) / "pending-apply"
    link.symlink_to(elsewhere)
    before, outside = _tree(work), _tree(elsewhere)
    with pytest.raises(ValueError):
        workspace_fork.pending_dir(work)
    # Whatever may be behind the link cannot be used: the restore names the link.
    with pytest.raises(workspace_fork.PendingApplyError) as err:
        workspace_fork.restore_pending(work)
    assert err.value.path == link
    with pytest.raises(ValueError):
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    assert _tree(work) == before and _tree(elsewhere) == outside
    assert (work.parent / "pending-apply").is_symlink()


def test_a_symlinked_journal_file_is_refused(work, tmp_path):
    real = tmp_path / "real-journal.json"
    real.write_text(json.dumps({"run_id": "r1", "node_id": "fan", "new_dirs": [],
                                "entries": [{"path": "a.txt", "pre": None, "mode": None}]}))
    pending = workspace_fork.pending_dir(work)
    pending.mkdir()
    (pending / "journal.json").symlink_to(real)
    before = _tree(work)
    with pytest.raises(workspace_fork.PendingApplyError):
        workspace_fork.restore_pending(work)
    assert _tree(work) == before and real.exists()


def test_without_a_journal_a_leftover_folder_is_removed(work):
    pending = workspace_fork.pending_dir(work)
    (pending / "pre").mkdir(parents=True)
    (pending / "pre" / "0").write_bytes(b"old")
    assert workspace_fork.restore_pending(work) is False
    assert not pending.exists()
    assert workspace_fork.restore_pending(work) is False     # nothing at all: still False


# ── what a write-back refuses ──

SHAPE = "would turn a file into a folder or a folder into a file"
LINKED = "is reached through a link"
IS_LINK = "is a link"
NOT_REGULAR = "is not a regular file"
OTHER_VOLUME = "is on another volume"


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "old.txt").write_text("outside old")
    os.chmod(outside / "old.txt", 0o640)
    (outside / "sentinel").write_text("keep")
    return outside


def _refusal_case(work: Path, tmp_path: Path, case: str):
    """Set up ``case`` in the folder; returns (changesets, refused path, reason)."""
    outside = _outside(tmp_path)
    if case == "a-file-and-a-file-inside-it":
        return [ChangeSet({"p": b"p"}, {}, set()), ChangeSet({"p/q": b"q"}, {}, set())], "p", SHAPE
    if case == "a-file-inside-a-file-first":
        return [ChangeSet({"p/q": b"q"}, {}, set()), ChangeSet({"p": b"p"}, {}, set())], "p", SHAPE
    if case == "b-write-onto-a-folder":
        return [ChangeSet({"sub": b"x"}, {}, set())], "sub", SHAPE
    if case == "b-write-onto-an-empty-folder":
        (work / "emptyd").mkdir()
        return [ChangeSet({"emptyd": b"x"}, {}, set())], "emptyd", SHAPE
    if case == "b-delete-a-folder":
        return [ChangeSet({}, {}, {"sub"})], "sub", SHAPE
    if case == "c-write-under-a-file":
        return [ChangeSet({"a.txt/y": b"y"}, {}, set())], "a.txt/y", SHAPE
    if case == "c-write-under-a-file-it-deletes":
        return ([ChangeSet({}, {}, {"a.txt"}), ChangeSet({"a.txt/y": b"y"}, {}, set())],
                "a.txt/y", SHAPE)
    if case == "fifo-at-a-written-path":
        if not hasattr(os, "mkfifo"):
            pytest.skip("no FIFOs here")
        os.mkfifo(work / "pipe")
        return [ChangeSet({}, {"pipe": b"x"}, set())], "pipe", NOT_REGULAR
    if case == "a-modified-leaf-link":
        (work / "ll").symlink_to(outside / "old.txt")
        return [ChangeSet({}, {"ll": b"n"}, set())], "ll", IS_LINK
    if case == "a-deleted-leaf-link":
        (work / "ll").symlink_to(outside / "old.txt")
        return [ChangeSet({}, {}, {"ll"})], "ll", IS_LINK
    if case == "a-new-file-at-a-link-to-nothing":
        (work / "dang").symlink_to(outside / "nothing-here")
        return [ChangeSet({"dang": b"n"}, {}, set())], "dang", IS_LINK
    if case == "a-leaf-link-to-an-inside-file":
        (work / "il").symlink_to("a.txt")
        return [ChangeSet({}, {"il": b"n"}, set())], "il", IS_LINK
    (work / "linkdir").symlink_to(outside)
    if case == "a-new-file-under-a-folder-link":
        return [ChangeSet({"linkdir/new.txt": b"n"}, {}, set())], "linkdir/new.txt", LINKED
    if case == "a-modified-file-under-a-folder-link":
        return [ChangeSet({}, {"linkdir/old.txt": b"n"}, set())], "linkdir/old.txt", LINKED
    if case == "a-deleted-file-under-a-folder-link":
        return [ChangeSet({}, {}, {"linkdir/old.txt"})], "linkdir/old.txt", LINKED
    if case == "under-a-link-to-an-inside-folder":
        (work / "inl").symlink_to("sub")
        return [ChangeSet({"inl/new.bin": b"n"}, {}, set())], "inl/new.bin", LINKED
    raise AssertionError(case)


REFUSALS = ["a-file-and-a-file-inside-it", "a-file-inside-a-file-first", "b-write-onto-a-folder",
            "b-write-onto-an-empty-folder", "b-delete-a-folder", "c-write-under-a-file",
            "c-write-under-a-file-it-deletes", "fifo-at-a-written-path", "a-modified-leaf-link",
            "a-deleted-leaf-link", "a-new-file-at-a-link-to-nothing",
            "a-leaf-link-to-an-inside-file", "a-new-file-under-a-folder-link",
            "a-modified-file-under-a-folder-link", "a-deleted-file-under-a-folder-link",
            "under-a-link-to-an-inside-folder"]


@pytest.mark.parametrize("case", REFUSALS)
def test_a_write_back_that_changes_a_shape_or_meets_a_link_or_a_special_file_is_refused(
        work, tmp_path, case):
    changesets, path, reason = _refusal_case(work, tmp_path, case)
    before, outside_before = _tree(work), _tree(tmp_path / "outside")
    with pytest.raises(workspace_fork.WriteBackRefusedError) as err:
        workspace_fork.apply_all(changesets, work, run_id="r1", node_id="fan")
    assert str(err.value) == f"{path!r} {reason}"
    assert err.value.path == path and err.value.reason == reason
    assert _tree(work) == before and _tree(tmp_path / "outside") == outside_before
    assert not os.path.lexists(workspace_fork.run_area(work) / "pending-apply")


def test_a_path_on_another_volume_is_refused(work, monkeypatch):
    (work / "mnt").mkdir()
    (work / "mnt" / "f.txt").write_bytes(b"f")
    before = _tree(work)
    real_lstat = os.lstat
    mounted = str(work / "mnt")

    def other_device(path, *a, **kw):
        st = real_lstat(path, *a, **kw)
        if os.fspath(path) == mounted:
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, *tuple(st)[3:10]))
        return st

    monkeypatch.setattr(os, "lstat", other_device)
    for cs, path in ((ChangeSet({}, {"mnt/f.txt": b"g"}, set()), "mnt/f.txt"),
                     (ChangeSet({"mnt/new/n.txt": b"n"}, {}, set()), "mnt/new/n.txt"),
                     (ChangeSet({}, {}, {"mnt/f.txt"}), "mnt/f.txt")):
        with pytest.raises(workspace_fork.WriteBackRefusedError) as err:
            workspace_fork.apply_all([cs], work, run_id="r1", node_id="fan")
        assert str(err.value) == f"{path!r} {OTHER_VOLUME}"
    monkeypatch.undo()
    assert _tree(work) == before
    assert not os.path.lexists(workspace_fork.run_area(work) / "pending-apply")


def test_a_file_and_a_file_inside_it_from_two_branches_is_a_union_conflict():
    a = ChangeSet({"p": b"p"}, {}, set())
    b = ChangeSet({"p/q/r": b"r"}, {}, set())
    assert workspace_fork.conflicts([a, b]) == {"p", "p/q/r"}
    assert workspace_fork.conflicts([b, a]) == {"p", "p/q/r"}
    assert workspace_fork.conflicts([ChangeSet({"p/q": b"1"}, {}, set()),
                                     ChangeSet({"p/r": b"2"}, {}, set())]) == set()


# ── the working folder itself replaced by a link ──

def _sentinels(outside: Path) -> None:
    for rel in ("a.txt", "exe.sh", "sub/b.bin", "new/n.txt"):
        (outside / rel).parent.mkdir(parents=True, exist_ok=True)
        (outside / rel).write_bytes(b"OUTSIDE " + rel.encode())
        os.chmod(outside / rel, 0o600)


FOLDER_IS_LINK = "the run's working folder is a link"
FOLDER_ON_OTHER_VOLUME = "the run's working folder is on another volume than the run's area"


def test_a_linked_working_folder_without_a_journal_is_left_alone_and_not_written_into(
        work, tmp_path):
    import shutil
    outside = tmp_path / "outside"
    _sentinels(outside)
    shutil.move(str(work), str(tmp_path / "kept-work"))
    work.symlink_to(outside)
    area = workspace_fork.run_area(work)
    before, area_before = _tree(outside), _tree(area)
    assert workspace_fork.restore_pending(work) is False          # nothing to undo
    assert _tree(area) == area_before                            # and nothing made
    with pytest.raises(workspace_fork.WriteBackRefusedError) as err:
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    assert str(err.value) == FOLDER_IS_LINK
    assert _tree(outside) == before
    assert not os.path.lexists(area / "pending-apply")


def test_a_working_folder_replaced_by_a_link_after_a_cut_is_not_undone_through_it(
        work, tmp_path, monkeypatch):
    import shutil
    outside = tmp_path / "outside"
    _sentinels(outside)
    _fail_after(monkeypatch, 2)
    with pytest.raises(OSError):
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    shutil.move(str(work), str(tmp_path / "kept-work"))
    work.symlink_to(outside)
    before = _tree(outside)
    with pytest.raises(workspace_fork.PendingApplyError) as err:
        workspace_fork.restore_pending(work)
    assert err.value.path == _journal(work)                # the file a person looks at
    assert _tree(outside) == before                       # nothing created, removed, written
    assert _journal(work).is_file()


def test_a_working_folder_on_another_volume_than_its_run_area_is_refused(work, monkeypatch):
    before = _tree(work)
    real_lstat = os.lstat

    def other_device(path, *a, **kw):
        st = real_lstat(path, *a, **kw)
        if os.fspath(path) == str(work):
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, *tuple(st)[3:10]))
        return st

    monkeypatch.setattr(os, "lstat", other_device)
    with pytest.raises(workspace_fork.WriteBackRefusedError) as err:
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    assert str(err.value) == FOLDER_ON_OTHER_VOLUME
    assert _tree(work) == before
    assert not os.path.lexists(workspace_fork.run_area(work) / "pending-apply")


# ── deletes of paths that do not exist, and an undo of what did not change ──

def test_a_delete_of_a_path_that_does_not_exist_is_neither_recorded_nor_undone(tmp_path,
                                                                               monkeypatch):
    cs = ChangeSet(created={"n1.txt": b"n"}, modified={"a.txt": b"a2"}, deleted={"ghost.txt"})
    for k in range(3):                         # two renames; the delete is not done
        work = _folder(tmp_path / f"k{k}")
        before = _tree(work)
        with monkeypatch.context() as m:
            _cut_folder_changes(m, work, k)
            try:
                workspace_fork.apply_all([cs], work, run_id="r1", node_id="fan")
            except OSError:
                pass
            else:
                assert k == 2, k
                assert not (work / "ghost.txt").exists()
                continue
        data = json.loads(_journal(work).read_text())
        assert "ghost.txt" not in {e["path"] for e in data["entries"]}
        (work / "ghost.txt").write_bytes(b"someone else's")
        assert workspace_fork.restore_pending(work) is True
        assert _tree(work) == {**before, "ghost.txt": (b"someone else's", 0o644)}


def _count_folder_changes(monkeypatch, work: Path) -> dict[str, int]:
    counts = {"replace": 0, "unlink": 0}
    for name in counts:
        real = getattr(os, name)

        def op(*args, _name=name, _real=real, **kwargs):
            target = os.fspath(args[1] if _name == "replace" else args[0])
            if target.startswith(str(work) + os.sep):
                counts[_name] += 1
            return _real(*args, **kwargs)

        monkeypatch.setattr(os, name, op)
    return counts


def test_an_undo_touches_only_the_paths_the_write_back_changed(tmp_path, monkeypatch):
    cs = ChangeSet(created={"n1.txt": b"n"}, modified={"a.txt": b"a2", "sub/b.bin": b"\x00"},
                   deleted=set())
    expected = {0: {"replace": 0, "unlink": 0}, 1: {"replace": 0, "unlink": 1},
                2: {"replace": 1, "unlink": 1}}
    for k, wanted in expected.items():
        work = _folder(tmp_path / f"k{k}")
        before = _tree(work)
        with monkeypatch.context() as m:
            _fail_after(m, k)
            with pytest.raises(OSError):
                workspace_fork.apply_all([cs], work, run_id="r1", node_id="fan")
        with monkeypatch.context() as m:
            counts = _count_folder_changes(m, work)
            assert workspace_fork.restore_pending(work) is True
        assert counts == wanted, k
        assert _tree(work) == before


# ── the order of a write-back and of its undo ──

def _cut_folder_changes(monkeypatch, work: Path, k: int) -> list[int]:
    """Make the (k+1)-th change to anything inside ``work`` (a rename into it, an
    unlink, a directory made or removed) raise, as a process that dies there would
    stop. Returns the count of changes attempted."""
    roots = (str(work) + os.sep, os.path.realpath(work) + os.sep)
    seen = [0]

    def guard(name, real, at):
        def op(*args, **kwargs):
            target = os.path.abspath(os.fspath(args[at]))
            if target.startswith(roots):
                seen[0] += 1
                if seen[0] > k:
                    raise OSError(f"cut at {name} {target}")
            return real(*args, **kwargs)
        return op

    for name, at in (("replace", 1), ("rename", 1), ("unlink", 0), ("rmdir", 0),
                     ("mkdir", 0)):
        monkeypatch.setattr(os, name, guard(name, getattr(os, name), at))
    return seen


def _mixed(base: Path) -> Path:
    """A folder where a write-back makes nested new folders, adds a file to an
    existing one, deletes files and edits files with their modes."""
    work = base / ".workflow" / "r1" / "work"
    (work / "d").mkdir(parents=True)
    os.chmod(work / "d", 0o750)
    (work / "x").write_bytes(b"x1")
    (work / "d" / "a").write_bytes(b"da")
    (work / "d" / "b").write_bytes(b"db")
    (work / "run.sh").write_bytes(b"#!/bin/sh\n")
    os.chmod(work / "run.sh", 0o755)
    (work / "a.txt").write_bytes(b"a1")
    os.chmod(work / "a.txt", 0o640)
    return work


MIXED = [ChangeSet(created={"n1/n2/new.txt": b"n"}, modified={"a.txt": b"a2"},
                   deleted={"d/a", "ghost"}),
         ChangeSet(created={"d/c": b"c", "top.txt": b"t"}, modified={"x": b"x2"},
                   deleted={"run.sh", "d/b"})]


def test_the_mixed_write_back_applies_whole(tmp_path):
    work = _mixed(tmp_path)
    workspace_fork.apply_all(MIXED, work, run_id="r1", node_id="fan")
    after = {k: v for k, v in _tree(work).items() if v[0] != "dir"}
    assert after == {"n1/n2/new.txt": (b"n", 0o644), "a.txt": (b"a2", 0o640),
                     "d/c": (b"c", 0o644), "top.txt": (b"t", 0o644), "x": (b"x2", 0o644)}
    assert not workspace_fork.pending_dir(work).exists()


def test_every_cut_of_a_mixed_write_back_is_put_back_whole(tmp_path, monkeypatch):
    cuts = 0
    for k in range(200):
        work = _mixed(tmp_path / f"k{k}")
        before = _tree(work)
        with monkeypatch.context() as m:
            _cut_folder_changes(m, work, k)
            try:
                workspace_fork.apply_all(MIXED, work, run_id="r1", node_id="fan")
            except OSError:
                cuts += 1
            else:
                break
        assert workspace_fork.restore_pending(work) is True, k
        assert _tree(work) == before, k
        assert not workspace_fork.pending_dir(work).exists(), k
    assert cuts >= 8                           # every delete, mkdir and staged rename was cut


def test_every_cut_of_the_undo_ends_the_same_when_it_runs_again(tmp_path, monkeypatch):
    import shutil
    combos = 0
    for k in range(200):
        base = tmp_path / f"k{k}"
        work = _mixed(base)
        with monkeypatch.context() as m:
            _cut_folder_changes(m, work, k)
            try:
                workspace_fork.apply_all(MIXED, work, run_id="r1", node_id="fan")
            except OSError:
                pass
            else:
                break
        cut_state = _tree(work)
        pending = workspace_fork.pending_dir(work)
        saved = base / "saved-pending"
        shutil.copytree(pending, saved, symlinks=True)
        for j in range(200):
            if j:                              # back to the state the cut apply left
                shutil.rmtree(work)
                shutil.rmtree(pending, ignore_errors=True)     # gone after a whole undo
                work.mkdir()
                _rebuild(work, cut_state)
                shutil.copytree(saved, pending, symlinks=True)
            before = _tree(_mixed(base / f"ref{k}-{j}"))
            with monkeypatch.context() as m:
                _cut_folder_changes(m, work, j)
                try:
                    workspace_fork.restore_pending(work)
                except workspace_fork.UndoError:
                    combos += 1
                else:
                    assert _tree(work) == before, (k, j)
                    break
            assert (pending / "journal.json").is_file(), (k, j)   # a cut undo keeps it
            assert workspace_fork.restore_pending(work) is True, (k, j)
            assert _tree(work) == before, (k, j)
            assert not pending.exists(), (k, j)
    assert combos >= 20


def _rebuild(root: Path, tree: dict[str, tuple]) -> None:
    for rel, entry in sorted(tree.items()):
        p = root / rel
        if entry[0] == "dir":
            p.mkdir(parents=True, exist_ok=True)
            os.chmod(p, entry[1])
        elif entry[0] == "link":
            p.symlink_to(entry[1])
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(entry[0])
            os.chmod(p, entry[1])


# ── what the undo leaves, and what it will not act through ──

def test_the_undo_leaves_a_created_folder_that_holds_something_else(work, monkeypatch):
    before = _tree(work)
    _fail_after(monkeypatch, 1)                # new/n.txt written, then cut
    with pytest.raises(OSError):
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    (work / "new" / "det-report.txt").write_bytes(b"a detached step's file")
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == {**before, "new": _tree(work)["new"],
                           "new/det-report.txt": (b"a detached step's file", 0o644)}
    assert not workspace_fork.pending_dir(work).exists()


def test_the_undo_leaves_a_link_put_where_a_new_file_was_and_its_target(work, tmp_path,
                                                                         monkeypatch):
    outside = _outside(tmp_path)
    before, outside_before = _tree(work), _tree(outside)
    _fail_after(monkeypatch, 1)
    with pytest.raises(OSError):
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    (work / "new" / "n.txt").unlink()
    (work / "new" / "n.txt").symlink_to(outside / "sentinel")
    with _warnings() as lines:
        assert workspace_fork.restore_pending(work) is True
    assert _left_paths(lines) == ["new/n.txt"]
    assert _tree(work) == {**before, "new": _tree(work)["new"],
                           "new/n.txt": ("link", str(outside / "sentinel"))}
    assert _tree(outside) == outside_before
    assert not workspace_fork.pending_dir(work).exists()


# ── the undo acts on a path only when it holds what the write-back left there ──

def _warnings():
    """Collect the messages of every warning logged inside the block."""
    from contextlib import contextmanager

    from loguru import logger

    @contextmanager
    def collect():
        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING",
                          format="{message}")
        try:
            yield lines
        finally:
            logger.remove(sink)
    return collect()


def _left_paths(lines: list[str]) -> list[str]:
    """The paths the one warning of an undo names as left, or [] when it logged none."""
    import ast
    import re
    found = [line for line in lines if "someone else changed" in line]
    assert len(found) <= 1, found
    if not found:
        return []
    match = re.search(r"left (\[.*?\]) as they are", found[0])
    assert match, found[0]
    return ast.literal_eval(match.group(1))


def _parents(rel: str) -> list[str]:
    """The folders above *rel*, outermost first (worked out here, not by the code
    under test)."""
    parts = rel.split("/")[:-1]
    return ["/".join(parts[:depth]) for depth in range(1, len(parts) + 1)]


def _someone_writes(work: Path, rel: str) -> None:
    """Someone else writes their own content at *rel*, making its folders as needed."""
    (work / rel).parent.mkdir(parents=True, exist_ok=True)
    (work / rel).write_bytes(b"someone else's " + rel.encode())
    os.chmod(work / rel, 0o600)


# Six folder changes in this order: the delete of exe.sh, the folder `new`, then the
# renames of new/n.txt, top.txt, a.txt and sub/b.bin.
Y3_CS = ChangeSet(created={"new/n.txt": b"n", "top.txt": b"t"},
                  modified={"a.txt": b"a2", "sub/b.bin": b"\x00"},
                  deleted={"exe.sh"})


def test_an_undo_leaves_every_path_someone_else_changed_after_a_cut(tmp_path, monkeypatch):
    written = {**Y3_CS.created, **Y3_CS.modified}
    seen = {"reached-write": 0, "unreached-write": 0, "deleted-and-recreated": 0,
            "unreached-create": 0}
    for k in range(6):
        work = _folder(tmp_path / f"k{k}")
        before = _tree(work)
        with monkeypatch.context() as m:
            _cut_folder_changes(m, work, k)
            with pytest.raises(OSError):
                workspace_fork.apply_all([Y3_CS], work, run_id="r1", node_id="fan")
        # What the cut left at each path, read from the folder, and one path of each
        # kind someone else then changes.
        kinds: dict[str, list[str]] = {kind: [] for kind in seen}
        for rel, content in sorted(written.items()):
            now = (work / rel).read_bytes() if (work / rel).is_file() else None
            if now == content:
                kinds["reached-write"].append(rel)
            elif rel in before:
                kinds["unreached-write"].append(rel)
            else:
                kinds["unreached-create"].append(rel)
        if not (work / "exe.sh").exists():
            kinds["deleted-and-recreated"].append("exe.sh")
        edited = [paths[0] for paths in kinds.values() if paths]
        for kind, paths in kinds.items():
            seen[kind] += bool(paths)
        for rel in edited:
            _someone_writes(work, rel)
        with _warnings() as lines:
            assert workspace_fork.restore_pending(work) is True, k
        assert sorted(_left_paths(lines)) == sorted(edited), k
        expected = {key: (v[0],) if v[0] == "dir" else v for key, v in before.items()}
        for rel in edited:
            expected[rel] = (b"someone else's " + rel.encode(), 0o600)
            for folder in _parents(rel):
                expected.setdefault(folder, ("dir",))
        assert _gen_view(work) == expected, k
        assert not workspace_fork.pending_dir(work).exists(), k
    assert all(seen.values()), seen


def test_an_undo_leaves_a_written_file_someone_deleted_and_skips_a_created_one(work,
                                                                             monkeypatch):
    before = _tree(work)
    _fail_after(monkeypatch, 3)                   # new/n.txt, top.txt, a.txt written
    with pytest.raises(OSError):
        workspace_fork.apply_all([Y3_CS], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    (work / "a.txt").unlink()                     # a stored file the write-back wrote
    (work / "top.txt").unlink()                   # a file the write-back created
    with _warnings() as lines:
        assert workspace_fork.restore_pending(work) is True
    assert _left_paths(lines) == ["a.txt"]
    assert _tree(work) == {k: v for k, v in before.items() if k != "a.txt"}
    assert not workspace_fork.pending_dir(work).exists()


@pytest.mark.parametrize("case", ["written-under-a-file", "deleted-under-a-missing-folder"])
def test_an_undo_where_a_folder_above_a_stored_file_changed(work, monkeypatch, case):
    import shutil
    before = _tree(work)
    cs = (ChangeSet({}, {"sub/b.bin": b"\x00", "a.txt": b"a2"}, set())
          if case == "written-under-a-file" else ChangeSet({}, {"a.txt": b"a2"}, {"sub/b.bin"}))
    k = 1 if case == "written-under-a-file" else 0
    _fail_after(monkeypatch, k)                   # sub/b.bin written or deleted, a.txt not
    with pytest.raises(OSError):
        workspace_fork.apply_all([cs], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    shutil.rmtree(work / "sub")
    if case == "written-under-a-file":
        (work / "sub").write_bytes(b"someone else's sub")
        os.chmod(work / "sub", 0o600)
    with _warnings() as lines:
        assert workspace_fork.restore_pending(work) is True
    if case == "written-under-a-file":
        # sub/b.bin cannot be put back without removing their file: it is left.
        assert _left_paths(lines) == ["sub/b.bin"]
        expected = {k: v for k, v in before.items() if not k.startswith("sub")}
        assert _tree(work) == {**expected, "sub": (b"someone else's sub", 0o600)}
    else:
        # A deleted file is put back where nothing is, making its folder again.
        assert _left_paths(lines) == []
        assert _gen_view(work) == _gen_view_of(before)
    assert not workspace_fork.pending_dir(work).exists()


def _gen_view_of(tree: dict[str, tuple]) -> dict[str, tuple]:
    return {k: (v[0],) if v[0] == "dir" else v for k, v in tree.items()}


def test_a_journal_entry_without_a_pre_key_is_a_path_that_did_not_exist(work):
    import hashlib
    (work / "zzz.txt").write_bytes(b"made by the apply")
    before = {k: v for k, v in _tree(work).items() if k != "zzz.txt"}
    _plant(work, {"run_id": "r1", "node_id": "fan", "new_dirs": [], "entries": [
        {"path": "zzz.txt", "written": hashlib.sha256(b"made by the apply").hexdigest()}]})
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == before


@pytest.mark.parametrize("cs,path", [
    (ChangeSet({}, {}, {"ns/f.txt"}), "ns/f.txt"),
    (ChangeSet({"ns/new.txt": b"n"}, {}, set()), "ns/new.txt"),
    (ChangeSet({}, {"ns/f.txt": b"f2"}, set()), "ns/f.txt"),
], ids=["delete", "create", "modify"])
def test_a_path_that_cannot_be_examined_refuses_the_write_back(work, cs, path):
    if os.geteuid() == 0:
        pytest.skip("permissions do not bind the superuser")
    (work / "ns").mkdir()
    (work / "ns" / "f.txt").write_bytes(b"f1")
    before = _tree(work)
    os.chmod(work / "ns", 0o600)                    # no search permission
    try:
        with pytest.raises(workspace_fork.WriteBackRefusedError) as err:
            workspace_fork.apply_all([cs], work, run_id="r1", node_id="fan")
        assert str(err.value).startswith(f"{path!r} cannot be examined: [Errno 13]"), err.value
        assert not os.path.lexists(workspace_fork.run_area(work) / "pending-apply")
    finally:
        os.chmod(work / "ns", 0o755)
    assert _tree(work) == before


def test_a_delete_under_a_path_that_is_a_file_refuses_the_write_back(work):
    before = _tree(work)
    with pytest.raises(workspace_fork.WriteBackRefusedError) as err:
        workspace_fork.apply_all([ChangeSet({}, {}, {"a.txt/y"})], work,
                                 run_id="r1", node_id="fan")
    assert str(err.value).startswith("'a.txt/y' cannot be examined: [Errno 20]"), err.value
    assert _tree(work) == before
    assert not os.path.lexists(workspace_fork.run_area(work) / "pending-apply")


@pytest.mark.parametrize("cs,k", [
    (ChangeSet({"ns/new.txt": b"n", "top.txt": b"t"}, {}, set()), 1),   # created, then cut
    (ChangeSet({}, {"ns/f.txt": b"f2", "a.txt": b"a2"}, set()), 1),   # modified, then cut
], ids=["created", "modified"])
def test_an_undo_that_cannot_examine_a_path_stops_and_runs_again_later(work, monkeypatch, cs, k):
    if os.geteuid() == 0:
        pytest.skip("permissions do not bind the superuser")
    (work / "ns").mkdir()
    (work / "ns" / "f.txt").write_bytes(b"f1")
    before = _tree(work)
    _fail_after(monkeypatch, k)
    with pytest.raises(OSError):
        workspace_fork.apply_all([cs], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    os.chmod(work / "ns", 0o600)
    try:
        with pytest.raises(workspace_fork.UndoError) as err:
            workspace_fork.restore_pending(work)
        assert "'ns/" in str(err.value.cause) and "cannot be examined" in str(err.value.cause)
        assert _journal(work).is_file()
    finally:
        os.chmod(work / "ns", 0o755)
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == before
    assert not workspace_fork.pending_dir(work).exists()


@pytest.mark.parametrize("cs", [
    [ChangeSet({}, {}, {"a.txt"}), ChangeSet({}, {"a.txt": b"a2"}, set())],
    [ChangeSet({}, {"a.txt": b"a2"}, set()), ChangeSet({}, {"a.txt": b"a3"}, set())],
], ids=["deleted-and-written", "written-twice"])
def test_a_path_touched_two_ways_is_not_written_back(work, cs):
    before = _tree(work)
    with pytest.raises(ValueError, match="'a.txt'"):
        workspace_fork.apply_all(cs, work, run_id="r1", node_id="fan")
    assert _tree(work) == before
    assert not os.path.lexists(workspace_fork.run_area(work) / "pending-apply")


def _swap_for_link(folder: Path, target: Path) -> Path:
    """Replace *folder* with a link to *target*; returns where its content was kept."""
    import shutil
    kept = folder.parent.parent / f"kept-{folder.name}"
    shutil.move(str(folder), str(kept))
    folder.symlink_to(target)
    return kept


def _swap_back(folder: Path, kept: Path) -> None:
    import shutil
    folder.unlink()
    shutil.move(str(kept), str(folder))


@pytest.mark.parametrize("case", ["stored-file", "created-folder"])
def test_the_undo_does_not_act_through_a_folder_swapped_for_a_link(work, tmp_path, monkeypatch,
                                                                     case):
    outside = _outside(tmp_path)
    (outside / "b.bin").write_bytes(b"OUTSIDE b")
    (outside / "x").write_text("an outside file named x")
    os.chmod(outside / "sentinel", 0o600)
    before = _tree(work)
    if case == "stored-file":
        cs, k, swapped, named = (ChangeSet({}, {"sub/b.bin": b"\x00", "a.txt": b"a2"}, set()),
                                 1, "sub", ["sub/b.bin"])
    else:
        cs, k, swapped, named = (ChangeSet({"nd/x": b"x", "a.txt": b"a2"}, {}, set()),
                                 1, "nd", ["nd", "nd/x"])
    _fail_after(monkeypatch, k)
    with pytest.raises(OSError):
        workspace_fork.apply_all([cs], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    kept = _swap_for_link(work / swapped, outside)
    outside_before = _tree(outside)
    with pytest.raises(workspace_fork.UndoError) as err:
        workspace_fork.restore_pending(work)
    assert _tree(outside) == outside_before          # bytes, modes and entries
    assert _journal(work).is_file()
    for path in named:
        assert repr(path) in str(err.value.cause)
    _swap_back(work / swapped, kept)
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == before and _tree(outside) == outside_before


def test_the_write_back_does_not_write_through_a_folder_swapped_for_a_link(work, tmp_path,
                                                                           monkeypatch):
    outside = _outside(tmp_path)
    (outside / "b.bin").write_bytes(b"OUTSIDE b")
    before = _tree(work)
    real = workspace_fork._write_file
    calls, kept = [0], []

    def swap_then_write(target, content, *rest):
        calls[0] += 1
        if calls[0] == 3:                             # before sub/b.bin is written
            kept.append(_swap_for_link(work / "sub", outside))
        return real(target, content, *rest)

    monkeypatch.setattr(workspace_fork, "_write_file", swap_then_write)
    outside_before = _tree(outside)
    with pytest.raises(OSError) as err:
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    assert "'sub/b.bin'" in str(err.value) and _tree(outside) == outside_before
    with pytest.raises(workspace_fork.UndoError):
        workspace_fork.restore_pending(work)
    assert _tree(outside) == outside_before
    _swap_back(work / "sub", kept[0])
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == before


# ── staging and renaming ──

def _listing(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def test_a_new_file_is_staged_outside_the_folder_and_renamed_into_place(work, monkeypatch):
    before = _listing(work)
    real_replace = os.replace
    seen: list[set[str]] = []

    def watch(src, dst, *a, **kw):
        if str(dst).startswith(str(work)):
            seen.append(_listing(work))
            assert str(src).startswith(str(workspace_fork.pending_dir(work) / "tmp")), src
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", watch)
    _fail_after(monkeypatch, 2)
    with pytest.raises(OSError):
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    assert workspace_fork.restore_pending(work) is True
    expected = before | {"new", "new/n.txt"}
    assert seen and all(names <= expected for names in seen), seen
    assert _listing(work) == before


def test_a_rename_that_fails_fails_the_write_back_and_it_is_undone(work, monkeypatch):
    before = _tree(work)
    real_replace = os.replace

    def other_device(src, dst, *a, **kw):
        if "pending-apply" in str(src) and str(dst).endswith("a.txt"):
            raise OSError(errno.EXDEV, "cross-device link")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", other_device)
    with pytest.raises(OSError) as err:
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    assert err.value.errno == errno.EXDEV
    # The undo needs no rename onto a.txt, which the write-back never changed: it
    # finishes although the rename still fails.
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == before


def test_a_read_only_folder_fails_the_write_back_and_leaves_no_journal(work):
    if os.geteuid() == 0:
        pytest.skip("permissions do not bind the superuser")
    (work / "ro").mkdir()
    (work / "ro" / "f.txt").write_bytes(b"f1")
    os.chmod(work / "ro", 0o555)
    try:
        before = _tree(work)
        for _ in range(2):                         # the same on every attempt
            with pytest.raises(PermissionError):
                workspace_fork.apply_all([ChangeSet({}, {"ro/f.txt": b"f2", "a.txt": b"a2"},
                                                    {"exe.sh"})],
                                         work, run_id="r1", node_id="fan")
            assert workspace_fork.restore_pending(work) is True
            assert _tree(work) == before
            assert not workspace_fork.pending_dir(work).exists()
    finally:
        os.chmod(work / "ro", 0o755)


# ── a seeded generator: every write-back is refused, applied whole, failed, or undone ──

def _gen_tree(rng, base: Path) -> tuple[Path, Path]:
    work = base / ".workflow" / "r1" / "work"
    outside = base / "outside"
    (outside / "od").mkdir(parents=True)
    (outside / "of.txt").write_bytes(b"of-" + bytes([rng.randrange(256)]))
    (outside / "od" / "x.txt").write_bytes(b"ox")
    (work / "d1" / "d2").mkdir(parents=True)
    (work / "empty").mkdir()
    for rel in ("f1.txt", "f2.bin", "d1/g.txt", "d1/d2/h.txt", "run.sh"):
        (work / rel).write_bytes(bytes(rng.randrange(256) for _ in range(rng.randrange(1, 9))))
        os.chmod(work / rel, rng.choice([0o600, 0o640, 0o644, 0o755]))
    (work / "ll").symlink_to(outside / "of.txt")
    (work / "dl").symlink_to(outside / "od")
    return work, outside


# Paths a write-back can take, and paths that make it refused (a shape change, a
# link, or a path under the folder link), drawn less often so most cases reach the
# folder.
_GEN_WRITES = ["n1.txt", "nd/x.txt", "nd/ne/y.txt", "d1/new.txt", "d1/d2/new2.txt",
               "empty/z.txt", "f1.txt", "f2.bin", "d1/g.txt", "d1/d2/h.txt", "run.sh"]
_GEN_BAD_WRITES = ["dl/new.txt", "dl/x.txt", "f1.txt/sub", "empty", "d1", "nd", "ll"]
_GEN_DELETES = ["f1.txt", "f2.bin", "d1/g.txt", "d1/d2/h.txt", "run.sh", "missing.txt"]
_GEN_BAD_DELETES = ["d1", "dl/x.txt", "ll"]


def _gen_pick(rng, good, bad):
    return rng.choice(bad) if rng.random() < 0.06 else rng.choice(good)


def _gen_changesets(rng) -> list[ChangeSet]:
    out = []
    for _ in range(rng.randrange(1, 4)):
        writes = {_gen_pick(rng, _GEN_WRITES, _GEN_BAD_WRITES):
                  bytes([rng.randrange(256)]) * rng.randrange(1, 5)
                  for _ in range(rng.randrange(0, 4))}
        deletes = {_gen_pick(rng, _GEN_DELETES, _GEN_BAD_DELETES)
                   for _ in range(rng.randrange(0, 3))}
        out.append(ChangeSet(created=writes, modified={}, deleted=deletes))
    # A path one branch deletes and another writes, or two branches write with
    # different content, is a union conflict and never reaches a write-back
    # (apply_all refuses it): every written path keeps the first content drawn for
    # it, and a delete of a written path is dropped.
    content: dict[str, bytes] = {}
    for cs in out:
        for rel, data in cs.created.items():
            content.setdefault(rel, data)
    return [ChangeSet(created={rel: content[rel] for rel in cs.created}, modified={},
                      deleted=cs.deleted - set(content)) for cs in out]


def _gen_view(root: Path) -> dict[str, tuple]:
    """_tree without directory modes (a new directory's mode follows the umask)."""
    return {k: (v[0],) if v[0] == "dir" else v for k, v in _tree(root).items()}


def _gen_expect_refused(before: dict[str, tuple], changesets) -> bool:
    """Whether the write-back must be refused, worked out from the tree before and
    the generator's own knowledge of its links, independently of the production
    check: a link or a path under the folder link, a write onto a folder, a write
    under a file, a write another write needs as a folder, a delete of a folder."""
    written = {rel for cs in changesets for rel in {**cs.created, **cs.modified}}
    deleted = {rel for cs in changesets for rel in cs.deleted
               if rel in before or rel.startswith("dl/")}
    for rel in written | deleted:
        if rel == "ll" or rel.startswith("dl/"):
            return True
        if before.get(rel, (None,))[0] == "dir":
            return True
    for rel in written:
        parts = rel.split("/")[:-1]
        ancestors = ["/".join(parts[:d]) for d in range(1, len(parts) + 1)]
        if any(before.get(a, ("dir",))[0] != "dir" for a in ancestors):
            return True
        if any(a in written for a in ancestors):
            return True
    return False


def _gen_applied(before: dict[str, tuple], changesets) -> dict[str, tuple]:
    tree = dict(before)
    for cs in changesets:
        for rel in cs.deleted:
            tree.pop(rel, None)
    for cs in changesets:
        for rel, content in {**cs.created, **cs.modified}.items():
            mode = tree[rel][1] if rel in tree else 0o644
            tree[rel] = (content, mode)
            parts = rel.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                tree.setdefault("/".join(parts[:depth]), ("dir",))
    return tree


_GEN_MODES = ["whole", "cut", "cut-the-undo", "foreign-file", "read-only-folder",
              "failing-rename", "someone-else", "undo-fails-once"]


def _gen_touched(before: dict[str, tuple], changesets) -> set[str]:
    """The paths a write-back records: every written path, and every deleted path
    that exists."""
    return ({rel for cs in changesets for rel in {**cs.created, **cs.modified}}
            | {rel for cs in changesets for rel in cs.deleted if rel in before})


def _gen_someone_else(rng, work: Path, before: dict[str, tuple], changesets,
                      expected: dict[str, tuple]) -> set[str]:
    """After a cut, someone else writes their own bytes at a random subset of the
    touched paths and of the files the write-back does not touch, wherever the
    folder above the path is a folder now. Updates *expected* with what they wrote
    and returns the touched paths among them: the ones the undo must name as left."""
    touched = _gen_touched(before, changesets)
    files = {rel for rel, v in before.items() if v[0] not in ("dir", "link")}
    edited = set()
    for rel in sorted(touched | files | {"missing.txt"}):
        if rel == "ll" or rel.startswith("dl/") or rng.random() >= 0.3:
            continue
        parent = (work / rel).parent
        if parent.is_symlink() or not parent.is_dir() or (work / rel).is_dir():
            continue
        if (work / rel).is_symlink():
            continue
        _someone_writes(work, rel)
        expected[rel] = (b"someone else's " + rel.encode(), 0o600)
        for folder in _parents(rel):
            expected.setdefault(folder, ("dir",))
        edited.add(rel)
    return edited & touched


def _gen_fail_one_path(m, work: Path, before: dict[str, tuple], changesets) -> str | None:
    """Make every rename onto, and every unlink of, one path the undo must act on
    fail (a path the cut write-back changed, read from the folder), for as long as
    *m* is active. Returns that path, or None when the cut changed nothing."""
    now = _gen_view(work)
    acting = sorted(rel for rel in _gen_touched(before, changesets) if now.get(rel) != before.get(rel))
    if not acting:
        return None
    target = str(work / acting[0])
    real_replace, real_unlink = os.replace, os.unlink

    def replace(src, dst, *a, **kw):
        if os.fspath(dst) == target:
            raise PermissionError(errno.EACCES, "refused", target)
        return real_replace(src, dst, *a, **kw)

    def unlink(path, *a, **kw):
        if os.fspath(path) == target:
            raise PermissionError(errno.EACCES, "refused", target)
        return real_unlink(path, *a, **kw)

    m.setattr(os, "replace", replace)
    m.setattr(os, "unlink", unlink)
    return acting[0]


@pytest.mark.parametrize("mode", _GEN_MODES)
def test_every_generated_write_back_is_refused_applied_failed_or_undone(tmp_path, monkeypatch,
                                                                       mode):
    import random
    if mode == "read-only-folder" and os.geteuid() == 0:
        pytest.skip("permissions do not bind the superuser")
    outcomes = {"refused": 0, "applied": 0, "undone": 0}
    special = 0                  # someone-else: cases with a path left; undo-fails-once: stops
    for seed in range(150):
        rng = random.Random(f"{mode}-{seed}")
        base = tmp_path / f"s{seed}"
        work, outside = _gen_tree(rng, base)
        changesets = _gen_changesets(rng)
        before, outside_before = _gen_view(work), _gen_view(outside)
        refuse = _gen_expect_refused(before, changesets)
        where = f"seed {mode}-{seed}: {changesets}"
        cut = (rng.randrange(0, 8) if mode in ("cut", "cut-the-undo", "foreign-file",
                                               "someone-else", "undo-fails-once") else None)
        # A path one changeset deletes and another writes is a union conflict and
        # never reaches a write-back, so the always-failing rename is drawn from
        # the written paths no changeset deletes.
        deleted = {rel for cs in changesets for rel in cs.deleted}
        written = [rel for cs in changesets for rel in {**cs.created, **cs.modified}
                   if rel not in deleted]
        failing = rng.choice(written) if mode == "failing-rename" and written else None
        if mode == "read-only-folder":
            os.chmod(work / "d1", 0o555)
        try:
            with monkeypatch.context() as m:
                if cut is not None:
                    _cut_folder_changes(m, work, cut)
                if failing is not None:
                    real_replace = os.replace

                    def refuse_one(src, dst, *a, _p=str(work / failing), **kw):
                        if os.fspath(dst) == _p:
                            raise PermissionError(errno.EACCES, "refused", _p)
                        return real_replace(src, dst, *a, **kw)
                    m.setattr(os, "replace", refuse_one)
                try:
                    workspace_fork.apply_all(changesets, work, run_id="r1", node_id="fan")
                except workspace_fork.WriteBackRefusedError:
                    outcome = "refused"
                except OSError:
                    outcome = "failed"
                else:
                    outcome = "applied"
                assert (outcome == "refused") == refuse, (outcome, where)
                if outcome == "failed" and cut is None:
                    # A persistent failure: the undo runs while it still holds.
                    assert workspace_fork.restore_pending(work) is True, where
            if outcome == "refused":
                assert _gen_view(work) == before, where
                assert not os.path.lexists(work.parent / "pending-apply"), where
            elif outcome == "applied":
                assert _gen_view(work) == _gen_applied(before, changesets), where
                assert not os.path.lexists(work.parent / "pending-apply"), where
            else:
                expected = dict(before)
                if mode == "foreign-file":
                    data = json.loads((work.parent / "pending-apply" / "journal.json").read_text())
                    made = [f for f in data["new_dirs"] if (work / f).is_dir()]
                    if made:
                        foreign = rng.choice(made) + "/foreign.txt"
                        (work / foreign).write_bytes(b"foreign")
                        expected[foreign] = (b"foreign", 0o644)
                        parts = foreign.split("/")[:-1]
                        for depth in range(1, len(parts) + 1):
                            expected.setdefault("/".join(parts[:depth]), ("dir",))
                warned: set[str] = set()
                if mode == "someone-else":
                    warned = _gen_someone_else(rng, work, before, changesets, expected)
                    special += bool(warned)
                if mode == "cut-the-undo":
                    with monkeypatch.context() as m:
                        _cut_folder_changes(m, work, rng.randrange(0, 6))
                        try:
                            workspace_fork.restore_pending(work)
                        except workspace_fork.UndoError:
                            pass
                        else:
                            assert _gen_view(work) == expected, where
                if mode == "undo-fails-once":
                    with monkeypatch.context() as m:
                        failing_path = _gen_fail_one_path(m, work, before, changesets)
                        if failing_path is not None:
                            try:
                                workspace_fork.restore_pending(work)
                            except workspace_fork.UndoError as exc:
                                stopped = str(exc.cause)
                            else:
                                stopped = "the undo did not stop"
                            assert repr(failing_path) in stopped, (stopped, where)
                            assert (work.parent / "pending-apply" / "journal.json").is_file(), where
                            special += 1
                with _warnings() as lines:
                    if os.path.lexists(work.parent / "pending-apply" / "journal.json"):
                        assert workspace_fork.restore_pending(work) is True, where
                assert set(_left_paths(lines)) == warned, where
                assert _gen_view(work) == expected, where
                assert not os.path.lexists(work.parent / "pending-apply"), where
                outcome = "undone"
            assert _gen_view(outside) == outside_before, where
            outcomes[outcome] += 1
        finally:
            if (work / "d1").is_dir():
                os.chmod(work / "d1", 0o755)
    assert outcomes["refused"] > 10, outcomes
    assert outcomes["applied" if mode == "whole" else "undone"] > 10, outcomes
    if mode in ("someone-else", "undo-fails-once"):
        assert special > 10, (special, outcomes)


# ── storing the earlier content ──

@pytest.mark.parametrize("which", ["first", "last"])
def test_a_cut_while_storing_leaves_the_folder_and_no_journal(work, monkeypatch, which):
    before = _tree(work)
    real = workspace_fork._store_copy
    calls = [0]
    total = 3                                  # a.txt, sub/b.bin and exe.sh exist

    def store(*args):
        calls[0] += 1
        if (which == "first" and calls[0] == 1) or (which == "last" and calls[0] == total):
            raise OSError("killed while storing")
        return real(*args)

    monkeypatch.setattr(workspace_fork, "_store_copy", store)
    with pytest.raises(OSError):
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    assert calls[0] == (1 if which == "first" else total)
    assert not _journal(work).exists()        # written only after every stored copy
    assert _tree(work) == before
    assert workspace_fork.restore_pending(work) is False
    assert not workspace_fork.pending_dir(work).exists()


# ── a case-insensitive filesystem ──

def test_a_case_only_rename_keeps_the_new_file_on_a_case_insensitive_filesystem(work):
    (work / "probe-case").write_text("p")
    if not (work / "PROBE-CASE").exists():
        pytest.skip("the test folder is on a case-sensitive filesystem")
    (work / "probe-case").unlink()
    workspace_fork.apply_all([ChangeSet(created={"A.txt": b"A!"}, modified={},
                                        deleted={"a.txt"})],
                             work, run_id="r1", node_id="fan")
    assert (work / "A.txt").read_bytes() == b"A!"
    assert "A.txt" in os.listdir(work)
