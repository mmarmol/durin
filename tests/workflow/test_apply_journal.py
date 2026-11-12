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
    assert err.value.path == _journal(work) and isinstance(err.value.cause, OSError)
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


def test_undoing_a_new_path_never_removes_what_a_link_there_points_at(work, tmp_path,
                                                                       monkeypatch):
    outside = tmp_path / "outside.txt"              # not there yet: the link dangles
    (work / "link.txt").symlink_to(outside)
    cs = ChangeSet(created={"link.txt": b"x", "a.txt": b"a2"}, modified={}, deleted=set())
    _fail_after(monkeypatch, 1)
    with pytest.raises(OSError):
        workspace_fork.apply_all([cs], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    assert outside.read_text() == "x"               # written through the link
    outside.write_text("someone else's")
    assert workspace_fork.restore_pending(work) is True
    assert outside.read_text() == "someone else's"  # outside the run's area: untouched
    assert os.readlink(work / "link.txt") == str(outside)    # the link itself stays
    assert (work / "a.txt").read_bytes() == b"a1"


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
    """A folder where a write-back turns a file into a directory, a directory into a
    file, makes nested new directories, deletes a script and edits a file."""
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


MIXED = [ChangeSet(created={}, modified={}, deleted={"x"}),
         ChangeSet(created={"x/y": b"y", "n1/n2/new.txt": b"n"}, modified={}, deleted={"x"}),
         ChangeSet(created={"d": b"d-file"}, modified={"a.txt": b"a2"},
                   deleted={"d/a", "d/b", "run.sh"})]


def test_a_file_that_becomes_a_directory_applies_whole(work):
    (work / "x").write_bytes(b"the person's x")
    # Branch A deleted x; branch B removed x and wrote x/y: not a conflict.
    workspace_fork.apply_all([ChangeSet(created={}, modified={}, deleted={"x"}),
                              ChangeSet(created={"x/y": b"y"}, modified={}, deleted={"x"})],
                             work, run_id="r1", node_id="fan")
    assert (work / "x" / "y").read_bytes() == b"y"
    assert not workspace_fork.pending_dir(work).exists()


def test_a_directory_that_becomes_a_file_applies_whole(work):
    (work / "dd").mkdir()
    (work / "dd" / "a").write_bytes(b"a")
    (work / "dd" / "b").write_bytes(b"b")
    workspace_fork.apply_all([ChangeSet(created={"dd": b"now a file"}, modified={},
                                        deleted={"dd/a", "dd/b"})],
                             work, run_id="r1", node_id="fan")
    assert (work / "dd").read_bytes() == b"now a file"
    assert stat.S_IMODE(os.stat(work / "dd").st_mode) == 0o644
    assert not workspace_fork.pending_dir(work).exists()


def test_the_mixed_write_back_applies_whole(tmp_path):
    work = _mixed(tmp_path)
    workspace_fork.apply_all(MIXED, work, run_id="r1", node_id="fan")
    after = _tree(work)
    assert {k: v for k, v in after.items() if v[0] != "dir"} == {
        "x/y": (b"y", 0o644), "n1/n2/new.txt": (b"n", 0o644), "d": (b"d-file", 0o644),
        "a.txt": (b"a2", 0o640)}
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
    assert cuts >= 10                          # every delete, rmdir, mkdir and write was cut


def test_every_cut_of_the_undo_ends_the_same_when_it_runs_again(tmp_path, monkeypatch):
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
        import shutil
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


# ── what the undo leaves alone, and links ──

def test_the_undo_never_removes_through_a_directory_link_leading_outside(work, tmp_path,
                                                                          monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (work / "linkdir").symlink_to(outside)
    cs = ChangeSet(created={"linkdir/new.txt": b"n", "a.txt": b"a2"}, modified={}, deleted=set())
    _fail_after(monkeypatch, 1)
    with pytest.raises(OSError):
        workspace_fork.apply_all([cs], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    assert (outside / "new.txt").read_bytes() == b"n"
    (outside / "new.txt").write_text("someone else's")
    assert workspace_fork.restore_pending(work) is True
    assert (outside / "new.txt").read_text() == "someone else's"
    assert os.readlink(work / "linkdir") == str(outside)


def test_an_outside_file_changed_through_a_directory_link_is_put_back(work, tmp_path,
                                                                      monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "old.txt").write_text("outside old")
    os.chmod(outside / "old.txt", 0o600)
    (work / "linkdir").symlink_to(outside)
    before, outside_before = _tree(work), _tree(outside)
    cs = ChangeSet(created={}, modified={"linkdir/old.txt": b"new", "a.txt": b"a2"},
                   deleted=set())
    _fail_after(monkeypatch, 1)
    with pytest.raises(OSError):
        workspace_fork.apply_all([cs], work, run_id="r1", node_id="fan")
    monkeypatch.undo()
    assert workspace_fork.restore_pending(work) is True
    assert _tree(work) == before and _tree(outside) == outside_before


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


# ── no temporary file in the working folder ──

def _listing(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


def test_no_temporary_file_ever_appears_in_the_working_folder(work, monkeypatch):
    before = _listing(work)
    real_replace = os.replace
    seen: list[set[str]] = []

    def watch(src, dst, *a, **kw):
        if str(dst).startswith(str(work)):
            seen.append(_listing(work))
            assert not str(src).startswith(str(work)), src     # staged outside the folder
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", watch)
    _fail_after(monkeypatch, 2)
    with pytest.raises(OSError):
        workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    assert workspace_fork.restore_pending(work) is True
    expected = before | {"new", "new/n.txt"}
    assert seen and all(names <= expected for names in seen), seen
    assert _listing(work) == before


def test_a_rename_onto_another_device_falls_back_to_the_atomic_write(work, monkeypatch):
    before = _tree(work)
    real_replace = os.replace
    refused: list[str] = []

    def other_device(src, dst, *a, **kw):
        if "pending-apply" in str(src) and str(dst).startswith(str(work)):
            refused.append(str(dst))
            raise OSError(errno.EXDEV, "cross-device link")
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", other_device)
    workspace_fork.apply_all([CS], work, run_id="r1", node_id="fan")
    assert (work / "a.txt").read_bytes() == b"a2" and (work / "new/n.txt").read_bytes() == b"n"
    assert stat.S_IMODE(os.stat(work / "new/n.txt").st_mode) == 0o644
    assert len(refused) == 3
    # The undo falls back the same way.
    work2 = _folder(work.parent.parent.parent / "second")
    before2 = _tree(work2)
    monkeypatch.setattr(os, "replace", real_replace)
    _fail_after(monkeypatch, 2)
    with pytest.raises(OSError):
        workspace_fork.apply_all([CS], work2, run_id="r1", node_id="fan")
    monkeypatch.undo()
    monkeypatch.setattr(os, "replace", other_device)
    assert workspace_fork.restore_pending(work2) is True
    assert _tree(work2) == before2
    assert before                              # the first folder's tree was taken


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
