"""Tests for per-branch workspace isolation + reconciliation (pure file logic)."""

import shutil
from pathlib import Path

from durin.workflow import workspace_fork as wf
from durin.workflow.workspace_fork import diff, fork, snapshot


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _work(tmp_path: Path) -> Path:
    """A working folder laid out as the engine lays it out: <tmp>/.workflow/<run>/work."""
    work = tmp_path / ".workflow" / "r1" / "work"
    work.mkdir(parents=True)
    return work


def _fork(work: Path) -> Path:
    return fork(work, into=wf.branches_root(work) / "r1")


def test_fork_of_a_working_folder_roundtrips_edits(tmp_path):
    # The engine forks the run's shared working folder directly: seeded files come
    # along, edits and additions diff cleanly against the base snapshot.
    work = _work(tmp_path)
    (work / "draft.md").write_text("v1")

    base = snapshot(work)
    assert "draft.md" in base

    fork_dir = _fork(work)
    try:
        assert (fork_dir / "draft.md").read_text() == "v1"   # seeded
        (fork_dir / "draft.md").write_text("v2")
        (fork_dir / "new.md").write_text("added")
        cs = diff(base, fork_dir)
        assert "draft.md" in cs.modified
        assert "new.md" in cs.created
    finally:
        shutil.rmtree(fork_dir, ignore_errors=True)


def test_fork_copies_files_and_excludes_heavy_dirs(tmp_path):
    work = _work(tmp_path)
    _write(work, "src/a.py", "print('a')")
    _write(work, ".git/HEAD", "ref: refs/heads/main")
    _write(work, "node_modules/x.js", "junk")
    fork = _fork(work)
    try:
        assert (fork / "src/a.py").read_text() == "print('a')"
        assert not (fork / ".git").exists()          # heavy/managed dirs excluded
        assert not (fork / "node_modules").exists()
    finally:
        shutil.rmtree(fork, ignore_errors=True)


def test_diff_detects_created_modified_deleted(tmp_path):
    work = _work(tmp_path)
    _write(work, "keep.txt", "keep")
    _write(work, "change.txt", "before")
    _write(work, "gone.txt", "remove me")
    base = wf.snapshot(work)

    fork = _fork(work)
    try:
        _write(fork, "new.txt", "brand new")          # created
        (fork / "change.txt").write_text("after")     # modified
        (fork / "gone.txt").unlink()                   # deleted
        cs = wf.diff(base, fork)
    finally:
        shutil.rmtree(fork, ignore_errors=True)

    assert set(cs.created) == {"new.txt"}
    assert cs.created["new.txt"] == b"brand new"
    assert set(cs.modified) == {"change.txt"}
    assert cs.deleted == {"gone.txt"}
    assert "keep.txt" not in cs.paths                 # untouched files are not changes


def test_conflicts_flags_divergent_writes_to_same_path(tmp_path):
    a = wf.ChangeSet(created={"x.py": b"a"}, modified={}, deleted=set())
    b = wf.ChangeSet(created={}, modified={"x.py": b"b"}, deleted=set())   # different content
    c = wf.ChangeSet(created={"y.py": b"c"}, modified={}, deleted=set())
    assert wf.conflicts([a, b, c]) == {"x.py"}        # a and b diverge on x.py
    assert wf.conflicts([a, c]) == set()              # disjoint -> no conflict


def test_identical_writes_to_same_path_are_not_a_conflict(tmp_path):
    # both branches emit the same incidental file (e.g. an empty __init__.py)
    a = wf.ChangeSet(created={"__init__.py": b"", "x.py": b"X"}, modified={}, deleted=set())
    b = wf.ChangeSet(created={"__init__.py": b"", "y.py": b"Y"}, modified={}, deleted=set())
    assert wf.conflicts([a, b]) == set()              # identical content -> reconciles cleanly


def test_write_vs_delete_on_same_path_conflicts(tmp_path):
    a = wf.ChangeSet(created={}, modified={"z.py": b"new"}, deleted=set())
    b = wf.ChangeSet(created={}, modified={}, deleted={"z.py"})
    assert wf.conflicts([a, b]) == {"z.py"}


def test_apply_writes_creates_and_modifications_and_removes_deletions(tmp_path):
    work = _work(tmp_path)
    _write(work, "old.txt", "old")
    cs = wf.ChangeSet(
        created={"sub/new.txt": b"new"},
        modified={"old.txt": b"updated"},
        deleted={"absent.txt"},
    )
    wf.apply_all([cs], work, run_id="r1", node_id="fan")
    assert (work / "sub/new.txt").read_text() == "new"
    assert (work / "old.txt").read_text() == "updated"
    # deleting a missing path is a no-op, not an error
    assert not (work / "absent.txt").exists()


def test_choose_applies_one_branch_discards_others(tmp_path):
    """End-to-end at the helper level: two branches each write the same artifact in
    isolation; 'choose' applies only the winner's changes to the real workspace."""
    work = _work(tmp_path)
    base = wf.snapshot(work)
    fork_a = _fork(work)
    fork_b = _fork(work)
    try:
        _write(fork_a, "result.txt", "approach A")
        _write(fork_b, "result.txt", "approach B")
        cs_a = wf.diff(base, fork_a)
        cs_b = wf.diff(base, fork_b)
        wf.apply_all([cs_b], work, run_id="r1", node_id="fan")   # judge picked B
    finally:
        shutil.rmtree(fork_a, ignore_errors=True)
        shutil.rmtree(fork_b, ignore_errors=True)
    assert (work / "result.txt").read_text() == "approach B"   # only the winner applied


def test_union_applies_disjoint_branches(tmp_path):
    work = _work(tmp_path)
    base = wf.snapshot(work)
    fork_a = _fork(work)
    fork_b = _fork(work)
    try:
        _write(fork_a, "module_x.py", "X")
        _write(fork_b, "module_y.py", "Y")
        cs_a = wf.diff(base, fork_a)
        cs_b = wf.diff(base, fork_b)
        assert wf.conflicts([cs_a, cs_b]) == set()    # disjoint
        wf.apply_all([cs_a, cs_b], work, run_id="r1", node_id="fan")
    finally:
        shutil.rmtree(fork_a, ignore_errors=True)
        shutil.rmtree(fork_b, ignore_errors=True)
    assert (work / "module_x.py").read_text() == "X"
    assert (work / "module_y.py").read_text() == "Y"
