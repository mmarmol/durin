"""Tests for per-branch workspace isolation + reconciliation (pure file logic)."""

from pathlib import Path

from durin.workflow import workspace_fork as wf


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_fork_copies_files_and_excludes_heavy_dirs(tmp_path):
    _write(tmp_path, "src/a.py", "print('a')")
    _write(tmp_path, ".git/HEAD", "ref: refs/heads/main")
    _write(tmp_path, "node_modules/x.js", "junk")
    fork = wf.fork(tmp_path)
    try:
        assert (fork / "src/a.py").read_text() == "print('a')"
        assert not (fork / ".git").exists()          # heavy/managed dirs excluded
        assert not (fork / "node_modules").exists()
    finally:
        wf.cleanup(fork)


def test_diff_detects_created_modified_deleted(tmp_path):
    _write(tmp_path, "keep.txt", "keep")
    _write(tmp_path, "change.txt", "before")
    _write(tmp_path, "gone.txt", "remove me")
    base = wf.snapshot(tmp_path)

    fork = wf.fork(tmp_path)
    try:
        _write(fork, "new.txt", "brand new")          # created
        (fork / "change.txt").write_text("after")     # modified
        (fork / "gone.txt").unlink()                   # deleted
        cs = wf.diff(base, fork)
    finally:
        wf.cleanup(fork)

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
    _write(tmp_path, "old.txt", "old")
    cs = wf.ChangeSet(
        created={"sub/new.txt": b"new"},
        modified={"old.txt": b"updated"},
        deleted={"absent.txt"},
    )
    wf.apply(cs, tmp_path)
    assert (tmp_path / "sub/new.txt").read_text() == "new"
    assert (tmp_path / "old.txt").read_text() == "updated"
    # deleting a missing path is a no-op, not an error
    assert not (tmp_path / "absent.txt").exists()


def test_choose_applies_one_branch_discards_others(tmp_path):
    """End-to-end at the helper level: two branches each write the same artifact in
    isolation; 'choose' applies only the winner's changes to the real workspace."""
    base = wf.snapshot(tmp_path)
    fork_a = wf.fork(tmp_path)
    fork_b = wf.fork(tmp_path)
    try:
        _write(fork_a, "result.txt", "approach A")
        _write(fork_b, "result.txt", "approach B")
        cs_a = wf.diff(base, fork_a)
        cs_b = wf.diff(base, fork_b)
        wf.apply(cs_b, tmp_path)        # judge picked B
    finally:
        wf.cleanup(fork_a)
        wf.cleanup(fork_b)
    assert (tmp_path / "result.txt").read_text() == "approach B"   # only the winner applied


def test_union_applies_disjoint_branches(tmp_path):
    base = wf.snapshot(tmp_path)
    fork_a = wf.fork(tmp_path)
    fork_b = wf.fork(tmp_path)
    try:
        _write(fork_a, "module_x.py", "X")
        _write(fork_b, "module_y.py", "Y")
        cs_a = wf.diff(base, fork_a)
        cs_b = wf.diff(base, fork_b)
        assert wf.conflicts([cs_a, cs_b]) == set()    # disjoint
        wf.apply(cs_a, tmp_path)
        wf.apply(cs_b, tmp_path)
    finally:
        wf.cleanup(fork_a)
        wf.cleanup(fork_b)
    assert (tmp_path / "module_x.py").read_text() == "X"
    assert (tmp_path / "module_y.py").read_text() == "Y"
