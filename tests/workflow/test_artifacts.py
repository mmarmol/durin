from pathlib import Path
from durin.workflow.artifacts import artifact_dir, prune_runs


def test_artifact_dir_is_keyed_by_run_node_iteration(tmp_path):
    d = artifact_dir(tmp_path, "run123", "make", 2)
    assert d == tmp_path / ".workflow" / "run123" / "make" / "2"
    assert d.is_dir()                                   # created


def test_artifact_root_gitignores_itself(tmp_path):
    artifact_dir(tmp_path, "r", "n", 1)
    assert (tmp_path / ".workflow" / ".gitignore").read_text() == "*\n"


def test_two_runs_do_not_collide(tmp_path):
    a = artifact_dir(tmp_path, "runA", "make", 1)
    b = artifact_dir(tmp_path, "runB", "make", 1)
    assert a != b and a.is_dir() and b.is_dir()


def test_prune_runs_keeps_last_n(tmp_path):
    root = tmp_path / ".workflow"
    for i in range(5):
        artifact_dir(tmp_path, f"run{i}", "n", 1)
    prune_runs(tmp_path, keep=2)
    remaining = {p.name for p in root.iterdir() if p.is_dir()}
    assert len(remaining) == 2                          # only the 2 newest run dirs survive
