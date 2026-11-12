"""Where a parallel branch's private copy lives: beside the run's working folder,
under the run's own area, so whatever removes the run's area removes the copies."""

import pytest

from durin.workflow import workspace_fork


def _work(tmp_path, run="r1"):
    work = tmp_path / ".workflow" / run / "work"
    work.mkdir(parents=True)
    return work


def test_a_copy_is_made_beside_the_working_folder_under_the_runs_name(tmp_path):
    work = _work(tmp_path)
    (work / "a.txt").write_text("a")
    copy = workspace_fork.fork(work, into=workspace_fork.branches_root(work) / "r1")
    assert copy.parent == tmp_path / ".workflow" / "r1" / "branches" / "r1"
    assert copy.name.startswith("b-") and (copy / "a.txt").read_text() == "a"


def test_a_keyed_runs_copies_live_beside_the_key_folders_work(tmp_path):
    work = tmp_path / ".workflow" / "keys" / "wf" / "k1" / "work"
    work.mkdir(parents=True)
    assert workspace_fork.branches_root(work) == work.parent / "branches"


def test_a_folder_outside_the_workflow_area_is_refused_and_nothing_is_removed(tmp_path):
    outside = tmp_path / "project" / "work"
    outside.mkdir(parents=True)
    keep = tmp_path / "project" / "branches" / "mine.txt"
    keep.parent.mkdir()
    keep.write_text("x")
    with pytest.raises(ValueError):
        workspace_fork.branches_root(outside)
    with pytest.raises(ValueError):
        workspace_fork.fork(outside, into=tmp_path / "project" / "branches" / "r1")
    workspace_fork.clear_branches(outside)            # best-effort: refuses silently
    workspace_fork.clear_branches(outside, "r1")
    assert keep.read_text() == "x"
    assert sorted(p.name for p in (tmp_path / "project").iterdir()) == ["branches", "work"]


def test_the_workflow_directory_itself_is_not_a_working_folder(tmp_path):
    area = tmp_path / ".workflow"
    area.mkdir()
    with pytest.raises(ValueError):
        workspace_fork.run_area(area)
    # a folder directly inside .workflow would put `branches` next to the run folders
    with pytest.raises(ValueError):
        workspace_fork.run_area(area / "work")


def test_a_copy_may_only_be_made_under_the_runs_branches_folder(tmp_path):
    work = _work(tmp_path)
    for elsewhere in (tmp_path / "elsewhere", work.parent / "other", work / "inside",
                      workspace_fork.branches_root(work).parent / "branches-x"):
        with pytest.raises(ValueError):
            workspace_fork.fork(work, into=elsewhere)
        assert not elsewhere.exists()
    # a path that climbs out of branches/ by `..` is not under it either
    sneaky = workspace_fork.branches_root(work) / ".." / ".." / "escape"
    with pytest.raises(ValueError):
        workspace_fork.fork(work, into=sneaky)
    assert not (tmp_path / ".workflow" / "escape").exists()


def test_clearing_one_run_leaves_another_runs_copies(tmp_path):
    work = _work(tmp_path)
    root = workspace_fork.branches_root(work)
    (root / "r1").mkdir(parents=True)
    (root / "r1" / "x").write_text("x")
    (root / "r2").mkdir()
    (root / "r2" / "y").write_text("y")
    workspace_fork.clear_branches(work, "r1")
    assert not (root / "r1").exists()
    assert (root / "r2" / "y").read_text() == "y"


def test_clearing_all_removes_every_runs_copies(tmp_path):
    work = _work(tmp_path)
    (work / "keep.txt").write_text("k")
    root = workspace_fork.branches_root(work)
    (root / "r1").mkdir(parents=True)
    (root / "r2").mkdir()
    workspace_fork.clear_branches(work)
    assert not root.exists()
    assert (work / "keep.txt").read_text() == "k"
    workspace_fork.clear_branches(work)               # nothing there: still quiet


def test_a_copy_does_not_contain_other_copies_or_the_resume_store(tmp_path):
    work = _work(tmp_path)
    (work / "a.txt").write_text("a")
    resume = work.parent / "resume"
    resume.mkdir()
    (resume / "state.json").write_text("{}")
    root = workspace_fork.branches_root(work)
    first = workspace_fork.fork(work, into=root / "r1")
    second = workspace_fork.fork(work, into=root / "r1")
    for copy in (first, second):
        assert sorted(p.name for p in copy.iterdir()) == ["a.txt"]
    assert workspace_fork.snapshot(work) == {"a.txt": workspace_fork._hash(b"a")}


def test_a_copy_is_made_exactly_under_branches_and_a_plain_run_id(tmp_path):
    work = _work(tmp_path)
    root = workspace_fork.branches_root(work)
    for bad in (root, root / "a" / "b", root / ".hidden", root / "bad name", root / ".."):
        with pytest.raises(ValueError):
            workspace_fork.fork(work, into=bad)
    assert not root.exists()


def _link_case(tmp_path, link_at):
    """A work folder whose `branches` (or `branches/r1`) is a symlink to an outside
    directory holding a sentinel; returns (work, outside, sentinel)."""
    work = _work(tmp_path)
    outside = tmp_path / "outside"
    (outside / "r1" / "left").mkdir(parents=True)
    sentinel = outside / "r1" / "left" / "keep.txt"
    sentinel.write_text("x")
    root = workspace_fork.branches_root(work)
    if link_at == "branches":
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        (root / "r1").symlink_to(outside / "r1", target_is_directory=True)
    return work, outside, sentinel


def _tree(path):
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*"))


@pytest.mark.parametrize("link_at", ["branches", "run"])
def test_a_symlink_at_branches_or_the_runs_copies_is_never_followed(tmp_path, link_at):
    work, outside, sentinel = _link_case(tmp_path, link_at)
    before = _tree(outside)
    with pytest.raises(ValueError):
        workspace_fork.fork(work, into=workspace_fork.branches_root(work) / "r1")
    workspace_fork.clear_branches(work, "r1")
    workspace_fork.clear_branches(work)
    assert sentinel.read_text() == "x" and _tree(outside) == before
