"""Writing a parallel step's result into the real working folder goes through an
undo journal: a write-back that was cut is put back whole before anything uses the
folder again (pure file logic on ``tmp_path``)."""
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


def _tree(root: Path) -> dict[str, tuple[bytes, int]]:
    """{relative path: (bytes, mode)} of every file under ``root``."""
    return {p.relative_to(root).as_posix(): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
            for p in sorted(root.rglob("*")) if p.is_file()}


def _fail_after(monkeypatch, k: int) -> list[int]:
    """Make the k+1-th real-file write of an apply raise; the first k go through."""
    real = workspace_fork._write_file
    calls = [0]

    def write(target, content):
        calls[0] += 1
        if calls[0] > k:
            raise OSError("disk full")
        return real(target, content)

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

    def cut(target, stored, mode):
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("killed")
        return real(target, stored, mode)

    monkeypatch.setattr(workspace_fork, "_put_back", cut)
    with pytest.raises(OSError):
        workspace_fork.restore_pending(work)
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
    assert not (work / "link.txt").is_symlink() and (work / "a.txt").read_bytes() == b"a1"


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
