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


def test_a_symlinked_file_round_trips_through_the_journal(work, tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_text("t1")
    (work / "link.txt").symlink_to(target)
    cs = ChangeSet(created={}, modified={"link.txt": b"t2", "a.txt": b"a2"}, deleted=set())
    _fail_after(monkeypatch, 1)
    with pytest.raises(OSError):
        workspace_fork.apply_all([cs], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    assert target.read_text() == "t2"        # the write-back went through the link
    assert workspace_fork.restore_pending(work) is True
    assert target.read_text() == "t1"
    assert (work / "link.txt").is_symlink() and (work / "a.txt").read_bytes() == b"a1"


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


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "old.txt").write_text("outside old")
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
    (work / "linkdir").symlink_to(outside)
    if case == "d-create-under-a-folder-link":
        return [ChangeSet({"linkdir/new.txt": b"n"}, {}, set())], "linkdir/new.txt", LINKED
    if case == "d-modify-under-a-folder-link":
        return [ChangeSet({}, {"linkdir/old.txt": b"n"}, set())], "linkdir/old.txt", LINKED
    if case == "d-delete-under-a-folder-link":
        return [ChangeSet({}, {}, {"linkdir/old.txt"})], "linkdir/old.txt", LINKED
    if case == "d-under-a-link-to-an-inside-folder":
        (work / "inl").symlink_to("sub")
        return [ChangeSet({"inl/new.bin": b"n"}, {}, set())], "inl/new.bin", LINKED
    if case == "e-write-through-a-link-to-nothing":
        (work / "dang").symlink_to(outside / "nothing-here")
        return [ChangeSet({}, {"dang": b"n"}, set())], "dang", LINKED
    raise AssertionError(case)


REFUSALS = ["a-file-and-a-file-inside-it", "a-file-inside-a-file-first", "b-write-onto-a-folder",
            "b-write-onto-an-empty-folder", "b-delete-a-folder", "c-write-under-a-file",
            "c-write-under-a-file-it-deletes", "d-create-under-a-folder-link",
            "d-modify-under-a-folder-link", "d-delete-under-a-folder-link",
            "d-under-a-link-to-an-inside-folder", "e-write-through-a-link-to-nothing"]


@pytest.mark.parametrize("case", REFUSALS)
def test_a_write_back_that_changes_a_shape_or_goes_through_a_link_is_refused(work, tmp_path,
                                                                             case):
    changesets, path, reason = _refusal_case(work, tmp_path, case)
    before, outside_before = _tree(work), _tree(tmp_path / "outside")
    with pytest.raises(workspace_fork.WriteBackRefusedError) as err:
        workspace_fork.apply_all(changesets, work, run_id="r1", node_id="fan")
    assert str(err.value) == f"{path!r} {reason}"
    assert err.value.path == path and err.value.reason == reason
    assert _tree(work) == before and _tree(tmp_path / "outside") == outside_before
    assert not os.path.lexists(workspace_fork.run_area(work) / "pending-apply")


def test_a_file_and_a_file_inside_it_from_two_branches_is_a_union_conflict():
    a = ChangeSet({"p": b"p"}, {}, set())
    b = ChangeSet({"p/q/r": b"r"}, {}, set())
    assert workspace_fork.conflicts([a, b]) == {"p", "p/q/r"}
    assert workspace_fork.conflicts([b, a]) == {"p", "p/q/r"}
    assert workspace_fork.conflicts([ChangeSet({"p/q": b"1"}, {}, set()),
                                     ChangeSet({"p/r": b"2"}, {}, set())]) == set()


# ── the order of a write-back and of its undo ──

def _cut_folder_changes(monkeypatch, work: Path, k: int) -> list[int]:
    """Make the (k+1)-th change to anything inside ``work`` (a rename into it, an
    unlink, a directory made or removed, a link made) raise, as a process that dies
    there would stop. Returns the count of changes attempted."""
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
                     ("mkdir", 0), ("symlink", 1)):
        monkeypatch.setattr(os, name, guard(name, getattr(os, name), at))
    return seen


def _mixed(base: Path) -> Path:
    """A folder where a write-back makes nested new folders, adds a file to an
    existing one, deletes files and a link, edits files, one through a link."""
    work = base / ".workflow" / "r1" / "work"
    (work / "d").mkdir(parents=True)
    os.chmod(work / "d", 0o750)
    (work / "x").write_bytes(b"x1")
    (work / "keep.txt").write_bytes(b"k1")
    os.chmod(work / "keep.txt", 0o600)
    (work / "d" / "a").write_bytes(b"da")
    (work / "d" / "b").write_bytes(b"db")
    (work / "run.sh").write_bytes(b"#!/bin/sh\n")
    os.chmod(work / "run.sh", 0o755)
    (work / "a.txt").write_bytes(b"a1")
    os.chmod(work / "a.txt", 0o640)
    (work / "rl").symlink_to("d/a")
    (work / "wl").symlink_to("keep.txt")
    return work


MIXED = [ChangeSet(created={"n1/n2/new.txt": b"n"}, modified={"a.txt": b"a2", "wl": b"k2"},
                   deleted={"d/a", "rl"}),
         ChangeSet(created={"d/c": b"c", "top.txt": b"t"}, modified={"x": b"x2"},
                   deleted={"run.sh", "d/b"})]


def test_the_mixed_write_back_applies_whole(tmp_path):
    work = _mixed(tmp_path)
    workspace_fork.apply_all(MIXED, work, run_id="r1", node_id="fan")
    after = {k: v for k, v in _tree(work).items() if v[0] != "dir"}
    assert after == {"n1/n2/new.txt": (b"n", 0o644), "a.txt": (b"a2", 0o640),
                     "keep.txt": (b"k2", 0o600), "wl": ("link", "keep.txt"),
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
        keep_cut = (work / "keep.txt").read_bytes()
        pending = workspace_fork.pending_dir(work)
        saved = base / "saved-pending"
        shutil.copytree(pending, saved, symlinks=True)
        for j in range(200):
            if j:                              # back to the state the cut apply left
                shutil.rmtree(work)
                shutil.rmtree(pending, ignore_errors=True)     # gone after a whole undo
                work.mkdir()
                _rebuild(work, cut_state)
                (work / "keep.txt").write_bytes(keep_cut)
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
    assert combos >= 30


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


def test_the_undo_removes_a_link_put_where_a_new_file_was_and_not_its_target(work, tmp_path,
                                                                              monkeypatch):
    outside = _outside(tmp_path)
    before, outside_before = _tree(work), _tree(outside)
    _fail_after(monkeypatch, 1)
    with pytest.raises(OSError):
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    (work / "new" / "n.txt").unlink()
    (work / "new" / "n.txt").symlink_to(outside / "sentinel")
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == before and _tree(outside) == outside_before


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


@pytest.mark.parametrize("case", ["stored-file", "deleted-link", "created-folder"])
def test_the_undo_does_not_act_through_a_folder_swapped_for_a_link(work, tmp_path, monkeypatch,
                                                                     case):
    outside = _outside(tmp_path)
    (outside / "b.bin").write_bytes(b"OUTSIDE b")
    (outside / "l").write_text("an outside file named l")
    (outside / "x").write_text("an outside file named x")
    os.chmod(outside / "sentinel", 0o600)
    (work / "par").mkdir()
    (work / "par" / "l").symlink_to("../a.txt")
    before = _tree(work)
    if case == "stored-file":
        cs, k, swapped, named = (ChangeSet({}, {"sub/b.bin": b"\x00", "a.txt": b"a2"}, set()),
                                 1, "sub", ["sub/b.bin"])
    elif case == "deleted-link":
        cs, k, swapped, named = (ChangeSet({}, {"a.txt": b"a2"}, {"par/l"}), 0, "par", ["par/l"])
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
    assert (work / "a.txt").read_bytes() == b"a1"    # what was safe was done
    _swap_back(work / swapped, kept)
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == before and _tree(outside) == outside_before


def test_a_write_through_a_link_waits_for_the_link_it_was_written_through(work, tmp_path,
                                                                         monkeypatch):
    outside = _outside(tmp_path)
    target = tmp_path / "target.txt"
    target.write_text("t1")
    (work / "ll").symlink_to(target)
    before = _tree(work)
    _fail_after(monkeypatch, 1)                       # ll written through, then cut
    with pytest.raises(OSError):
        workspace_fork.apply_all([ChangeSet({}, {"ll": b"t2", "a.txt": b"a2"}, set())], work,
                                 run_id="r1", node_id="fan")
    monkeypatch.undo()
    assert target.read_text() == "t2" and (work / "ll").is_symlink()
    (work / "ll").unlink()
    (work / "ll").symlink_to(outside / "sentinel")    # someone points it elsewhere
    outside_before = _tree(outside)
    with pytest.raises(workspace_fork.UndoError) as err:
        workspace_fork.restore_pending(work)
    assert "'ll'" in str(err.value.cause) and _tree(outside) == outside_before
    (work / "ll").unlink()
    (work / "ll").symlink_to(target)
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == before and target.read_text() == "t1"


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


def test_a_deleted_link_comes_back_as_the_same_link(work, tmp_path, monkeypatch):
    target = tmp_path / "target.txt"
    target.write_text("t1")
    (work / "link.txt").symlink_to(target)
    (work / "rel-link").symlink_to("sub/b.bin")
    before = _tree(work)
    cs = ChangeSet(created={}, modified={"a.txt": b"a2"}, deleted={"link.txt", "rel-link"})
    _fail_after(monkeypatch, 1)                # both links deleted, a.txt written, then cut
    with pytest.raises(OSError):
        workspace_fork.apply_all([cs, CS2], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    assert not os.path.lexists(work / "link.txt")
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == before and target.read_text() == "t1"


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
    monkeypatch.undo()
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == before


# ── a seeded generator: every write-back is refused, applied whole, or undone ──

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


# Paths a write-back can take, and paths that make it refused (a shape change or a
# path under the folder link), drawn less often so most cases reach the folder.
_GEN_WRITES = ["n1.txt", "nd/x.txt", "nd/ne/y.txt", "d1/new.txt", "d1/d2/new2.txt",
               "empty/z.txt", "f1.txt", "f2.bin", "d1/g.txt", "d1/d2/h.txt", "run.sh", "ll"]
_GEN_BAD_WRITES = ["dl/new.txt", "dl/x.txt", "f1.txt/sub", "empty", "d1", "nd"]
_GEN_DELETES = ["f1.txt", "f2.bin", "d1/g.txt", "run.sh", "ll", "missing.txt"]
_GEN_BAD_DELETES = ["d1", "dl/x.txt"]


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
    return out


def _gen_view(root: Path) -> dict[str, tuple]:
    """_tree without directory modes (a new directory's mode follows the umask)."""
    return {k: (v[0],) if v[0] == "dir" else v for k, v in _tree(root).items()}


def _gen_applied(before: dict[str, tuple], changesets, outside_file: dict) -> dict[str, tuple]:
    tree = dict(before)
    for cs in changesets:
        for rel in cs.deleted:
            if tree.get(rel, ("dir",))[0] != "dir":
                del tree[rel]
    for cs in changesets:
        for rel, content in {**cs.created, **cs.modified}.items():
            if tree.get(rel, (None,))[0] == "link":
                outside_file["of.txt"] = content
                continue
            mode = tree[rel][1] if rel in tree else 0o644
            tree[rel] = (content, mode)
            parts = rel.split("/")[:-1]
            for depth in range(1, len(parts) + 1):
                tree.setdefault("/".join(parts[:depth]), ("dir",))
    return tree


@pytest.mark.parametrize("mode", ["whole", "cut", "cut-the-undo", "foreign-file"])
def test_every_generated_write_back_is_refused_applied_or_undone(tmp_path, monkeypatch, mode):
    import random
    outcomes = {"refused": 0, "applied": 0, "undone": 0}
    for seed in range(150):
        rng = random.Random(f"{mode}-{seed}")
        base = tmp_path / f"s{seed}"
        work, outside = _gen_tree(rng, base)
        changesets = _gen_changesets(rng)
        before, outside_before = _gen_view(work), _gen_view(outside)
        where = f"seed {mode}-{seed}: {changesets}"
        cut = None if mode == "whole" else rng.randrange(0, 8)
        with monkeypatch.context() as m:
            if cut is not None:
                _cut_folder_changes(m, work, cut)
            try:
                workspace_fork.apply_all(changesets, work, run_id="r1", node_id="fan")
            except workspace_fork.WriteBackRefusedError:
                outcome = "refused"
            except OSError:
                outcome = "cut"
            else:
                outcome = "applied"
        if outcome == "refused":
            assert _gen_view(work) == before, where
            assert not os.path.lexists(work.parent / "pending-apply"), where
            assert _gen_view(outside) == outside_before, where
        elif outcome == "applied":
            out_file = {}
            assert _gen_view(work) == _gen_applied(before, changesets, out_file), where
            assert not os.path.lexists(work.parent / "pending-apply"), where
            expected_out = dict(outside_before)
            if "of.txt" in out_file:
                expected_out["of.txt"] = (out_file["of.txt"], outside_before["of.txt"][1])
            assert _gen_view(outside) == expected_out, where
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
            if mode == "cut-the-undo":
                with monkeypatch.context() as m:
                    _cut_folder_changes(m, work, rng.randrange(0, 6))
                    try:
                        workspace_fork.restore_pending(work)
                    except workspace_fork.UndoError:
                        pass
                    else:
                        assert _gen_view(work) == expected, where
            if os.path.lexists(work.parent / "pending-apply" / "journal.json"):
                assert workspace_fork.restore_pending(work) is True, where
            assert _gen_view(work) == expected, where
            assert not os.path.lexists(work.parent / "pending-apply"), where
            assert _gen_view(outside) == outside_before, where
            outcome = "undone"
        outcomes[outcome] += 1
    # The generator reaches every outcome the mode allows.
    assert outcomes["refused"] > 10, outcomes
    assert outcomes["applied" if mode == "whole" else "undone"] > 10, outcomes


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
